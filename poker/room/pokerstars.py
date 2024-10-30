import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import logging

import attr
import pytz
from lxml import etree
from zope.interface import implementer

from .. import handhistory as hh
from ..card import Card
from ..constants import Action, Currency, Game, GameType, Limit, MoneyType
from ..hand import Combo

__all__ = ["PokerStarsHandHistory", "Notes"]

# Configure logging
logging.basicConfig(
    filename='parser_errors.log',
    filemode='a',
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.ERROR
)

@implementer(hh.IStreet)
class _Street(hh._BaseStreet):
    def _parse_cards(self, boardline):
        """
        Parses the boardline to extract community cards.
        Example boardline: "*** FLOP *** [4c Js 7c]"
        """
        match = re.search(r'\[([^\]]+)\]', boardline)
        if match:
            card_str = match.group(1).split()
            self.cards = tuple(Card(cs) for cs in card_str[:3])  # Flop
            if len(card_str) > 3:
                self.turn = Card(card_str[3])
            else:
                self.turn = None
            if len(card_str) > 4:
                self.river = Card(card_str[4])
            else:
                self.river = None
        else:
            self.cards = ()
            self.turn = None
            self.river = None

    def _parse_actions(self, actionlines):
        """
        Parses action lines within a street.
        """
        actions = []
        for line in actionlines:
            if line.startswith("Uncalled bet"):
                action = self._parse_uncalled(line)
            elif "collected" in line:
                action = self._parse_collected(line)
            elif "doesn't show hand" in line or "mucks" in line:
                action = self._parse_muck(line)
            elif "joins the table" in line:
                self._handle_player_join(line)
                continue  # Skip adding to actions
            elif ' said, "' in line:  # Skip chat lines
                continue
            elif ":" in line:
                action = self._parse_player_action(line)
            else:
                logging.error(f"Bad action line: {line}")
                continue  # Skip bad lines without raising
            if action:
                actions.append(hh._PlayerAction(*action))
        self.actions = tuple(actions) if actions else None

    def _parse_uncalled(self, line):
        """
        Parses an 'Uncalled bet' line.
        Example: "Uncalled bet ($0.40) returned to EsAyy"
        """
        try:
            amount_match = re.search(r'\((\$?\d+(?:\.\d+)?)\)', line)
            name_match = re.search(r'to (\w+)', line)
            if amount_match and name_match:
                amount = Decimal(amount_match.group(1).replace('$', ''))
                name = name_match.group(1)
                return name, Action.RETURN, amount
            else:
                logging.error(f"Failed to parse Uncalled bet line: {line}")
                return None
        except Exception as e:
            logging.error(f"Error parsing Uncalled bet: {e}, Line: {line}")
            return None

    def _parse_collected(self, line):
        """
        Parses a 'collected' line.
        Example: "EsAyy collected $0.37 from pot"
        """
        try:
            match = re.match(r"^(?P<name>\w+) collected \$(?P<amount>\d+(?:\.\d+)?) from pot", line)
            if match:
                name = match.group("name")
                amount = Decimal(match.group("amount"))
                self.pot = amount
                return name, Action.WIN, self.pot
            else:
                logging.error(f"Failed to parse collected line: {line}")
                return None
        except Exception as e:
            logging.error(f"Error parsing collected line: {e}, Line: {line}")
            return None

    def _parse_muck(self, line):
        """
        Parses a 'mucks' line.
        Example: "PlayerX: doesn't show hand"
        """
        try:
            match = re.match(r"^(?P<name>\w+): (doesn't show hand|mucks)", line)
            if match:
                name = match.group("name")
                return name, Action.MUCK, None
            else:
                logging.error(f"Failed to parse muck line: {line}")
                return None
        except Exception as e:
            logging.error(f"Error parsing muck line: {e}, Line: {line}")
            return None

    def _parse_player_action(self, line):
        """
        Parses a player action line.
        Example: "iskander755: raises $0.10 to $0.15"
        """
        try:
            name, _, action_part = line.partition(": ")
            action_parts = action_part.split()
            action = action_parts[0].upper()

            # Handle actions with multiple words like "ALL-IN"
            if action == "ALL-IN":
                mapped_action = Action.ALL_IN
                amount = None
            elif action in ["BET", "CALL", "CHECK", "FOLD", "RAISE", "POSTS"]:
                mapped_action = Action[action]
                # Extract amount if present
                if len(action_parts) > 1:
                    amount_str = action_parts[1].replace('$', '').replace(',', '')
                    try:
                        amount = Decimal(amount_str)
                    except:
                        amount = None
                else:
                    amount = None
            else:
                logging.error(f"Unknown action type: {action} in line: {line}")
                return None

            return name, mapped_action, amount
        except Exception as e:
            logging.error(f"Error parsing player action: {e}, Line: {line}")
            return None

    def _handle_player_join(self, line):
        """
        Parses a line where a player joins the table mid-hand.
        Example: "Hergenschall joins the table at seat #1"
        """
        try:
            match = re.match(r"^(?P<name>.+?) joins the table at seat #(?P<seat>\d+)$", line)
            if match:
                name = match.group("name")
                seat = int(match.group("seat"))
                # Check if seat is already occupied
                if self.players[seat - 1].name and self.players[seat - 1].name != "Empty Seat {}".format(seat):
                    logging.warning(f"Seat {seat} already occupied. Player {name} cannot join.")
                else:
                    self.players[seat - 1] = hh._Player(
                        name=name,
                        stack=Decimal('0.00'),  # Default stack, adjust as necessary
                        seat=seat,
                        combo=None,
                    )
                    logging.info(f"Player {name} joined at seat {seat}.")
            else:
                logging.error(f"Failed to parse player join line: {line}")
        except Exception as e:
            logging.error(f"Error handling player join: {e}, Line: {line}")

@implementer(hh.IHandHistory)
class PokerStarsHandHistory(hh._SplittableHandHistoryMixin, hh._BaseHandHistory):
    """Parses PokerStars Tournament and Cash game hands."""

    _DATE_FORMAT = "%Y/%m/%d %H:%M:%S %Z"
    _TZ = pytz.timezone("US/Eastern")  # ET
    _split_re = re.compile(r"\*\*\*")  # Split sections based on '***'
    _header_re = re.compile(
        r"""
            ^PokerStars\s+                                # Poker Room
            Hand\s+\#(?P<ident>\d+):\s+                   # Hand history id
            (Tournament\s+\#(?P<tournament_ident>\d+),\s+ # Tournament Number
             (?:
                (?P<freeroll>Freeroll)                   # Freeroll
                |
                \$(?P<buyin>\d+(?:\.\d+)?)               # Buyin
                (?:\+\$(?P<rake>\d+(?:\.\d+)?)?)         # Rake (optional)
                (?:\s+(?P<currency>[A-Z]+))?            # Currency (optional)
             )\s+
            )?
            (?P<game>.+?)\s+                              # Game
            (?P<limit>(?:Pot\s+|No\s+|)Limit)\s+          # Limit
            (?:-\s+Level\s+(?P<tournament_level>\S+)\s+)?   # Level (optional)
            \(
             (?:
                (?P<sb>\d+)/(?P<bb>\d+)                     # Tournament blinds
                |
                \$(?P<cash_sb>\d+(?:\.\d+)?)/\$?(?P<cash_bb>\d+(?:\.\d+)?) # Cash blinds
                (?:\s+(?P<cash_currency>\S+))?               # Cash currency
             )
            \)\s+
            -\s+(?P<date>\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} \w{2}) # Date following hyphen
        """,
        re.VERBOSE,
    )
    _table_re = re.compile(
        r"^Table '(?P<table_name>.+)' (?P<max_players>\d+)-max Seat #(?P<button>\d+) is the button$"
    )
    _seat_re = re.compile(
        r"^Seat (?P<seat>\d+): (?P<name>.+?) \(\$?(?P<stack>\d+(?:\.\d+)?) in chips\)$"
    )
    _hero_re = re.compile(r"^Dealt to (?P<hero_name>.+?) \[(?P<card1>.{2}) (?P<card2>.{2})\]$")
    _pot_re = re.compile(r"^Total pot \$?(?P<pot>\d+(?:\.\d+)?) \| Rake \$?(?P<rake>\d+(?:\.\d+)?)$")
    _winner_re = re.compile(r"^Seat \d+: (?P<name>.+?) collected \(\$?(?P<amount>\d+(?:\.\d+)?)\)$")
    _showdown_re = re.compile(r"^Seat \d+: (?P<name>.+?) showed \[.+?\] and won$")
    _ante_re = re.compile(r".*posts the ante \$?(?P<ante>\d+(?:\.\d+)?)$")
    _board_re = re.compile(r"\[([^\]]+)\]")

    def parse_header(self):
        """
        Parses the header of the hand history.
        """
        # Split the raw hand history into sections based on '***'
        self._split_raw()

        # Extract the first line of the header section for regex matching
        header_section = self._splitted[0].strip().split('\n')[0]

        match = self._header_re.match(header_section)
        if not match:
            logging.error("Header does not match expected format.")
            raise RuntimeError("Header does not match expected format.")

        self.extra = dict()
        self.ident = match.group("ident")

        # Blinds
        self.sb = Decimal(match.group("sb") or match.group("cash_sb") or '0.00')
        self.bb = Decimal(match.group("bb") or match.group("cash_bb") or '0.00')

        # Tournament or Cash Game
        if match.group("tournament_ident"):
            self.game_type = GameType.TOUR
            self.tournament_ident = match.group("tournament_ident")
            self.tournament_level = match.group("tournament_level")

            self.buyin = Decimal(match.group("buyin") or '0.00')
            self.rake = Decimal(match.group("rake") or '0.00')
            currency = match.group("currency")
        else:
            self.game_type = GameType.CASH
            self.tournament_ident = None
            self.tournament_level = None
            self.buyin = None
            self.rake = None
            currency = match.group("cash_currency")

        # Currency and Money Type
        if match.group("freeroll") and not currency:
            currency = "USD"

        if not currency:
            self.extra["money_type"] = MoneyType.PLAY
            self.currency = None
        else:
            self.extra["money_type"] = MoneyType.REAL
            try:
                self.currency = Currency(currency)
            except ValueError:
                logging.error(f"Unknown currency: {currency}")
                self.currency = None

        # Game and Limit
        game_str = match.group("game").upper()
        try:
            self.game = Game(game_str)
        except ValueError:
            logging.error(f"Unknown game type: {game_str}")
            self.game = Game.UNKNOWN

        limit_str = match.group("limit").upper()
        try:
            self.limit = Limit(limit_str.replace(" ", "_"))  # Replace space with underscore if any
        except ValueError:
            logging.error(f"Unknown limit type: {limit_str}")
            self.limit = Limit.UNKNOWN

        # Parse Date
        date_str = match.group("date")
        try:
            naive_date = datetime.strptime(date_str, self._DATE_FORMAT)
            localized_date = self._TZ.localize(naive_date)
            self.date = localized_date.astimezone(pytz.UTC)
        except Exception as e:
            logging.error(f"Error parsing date: {e}, Date String: {date_str}")
            self.date = None

        self.header_parsed = True

    def parse(self):
        """
        Parses the entire hand history.
        """
        if not self.header_parsed:
            self.parse_header()

        self._parse_table()
        self._parse_players()
        self._parse_button()
        self._parse_hero()
        self._parse_preflop()
        self._parse_flop()
        self._parse_street("turn")
        self._parse_street("river")
        self._parse_showdown()
        self._parse_pot()
        self._parse_board()
        self._parse_winners()

        self._del_split_vars()
        self.parsed = True

    def _parse_table(self):
        """
        Parses the table information section.
        """
        try:
            table_section = self.sections.get("HEADER", "").strip().split('\n')[0]
            match = self._table_re.match(table_section)
            if not match:
                logging.error("Table section does not match expected format.")
                raise RuntimeError("Table section does not match expected format.")
            self.table_name = match.group("table_name")
            self.max_players = int(match.group("max_players"))
        except Exception as e:
            logging.error(f"Error parsing table section: {e}")
            raise

    def _parse_players(self):
        """
        Parses the players' information.
        """
        try:
            players_section = self.sections.get("HEADER", "").strip().split('\n')[1:]  # Skip the table info line
            self.players = self._init_seats(self.max_players)
            for line in players_section:
                match = self._seat_re.match(line.strip())
                if not match:
                    break  # End of players section
                seat = int(match.group("seat"))
                name = match.group("name")
                stack = Decimal(match.group("stack"))
                self.players[seat - 1] = hh._Player(
                    name=name,
                    stack=stack,
                    seat=seat,
                    combo=None,
                )
        except Exception as e:
            logging.error(f"Error parsing players section: {e}")
            raise

    def _parse_button(self):
        """
        Identifies the player on the button.
        """
        try:
            button_seat = int(self._table_match.group("button"))
            self.button = self.players[button_seat - 1]
        except Exception as e:
            logging.error(f"Error identifying button: {e}")
            self.button = None

    def _parse_hero(self):
        """
        Parses the hero's hole cards if present.
        """
        try:
            # Search for the "Dealt to" line in the HEADER section
            preflop_section = self.sections.get("HEADER", "").strip().split('\n')
            for line in preflop_section:
                match = self._hero_re.match(line.strip())
                if match:
                    hero_name = match.group("hero_name")
                    card1 = match.group("card1")
                    card2 = match.group("card2")
                    hero, hero_index = self._get_hero_from_players(hero_name)
                    hero.combo = Combo(card1 + card2)
                    self.hero = self.players[hero_index] = hero
                    if self.button.name == self.hero.name:
                        self.button = hero
                    break
            else:
                # If no "Dealt to" line is found, set hero to None or handle accordingly
                self.hero = None
        except Exception as e:
            logging.error(f"Error parsing hero information: {e}")
            self.hero = None

    def _parse_preflop(self):
        """
        Parses preflop actions.
        """
        try:
            preflop_section = self.sections.get("HOLE_CARDS", "").strip().split('\n')[1:]  # Skip 'HOLE CARDS' line
            preflop_actions = []
            for line in preflop_section:
                if line.upper().startswith("***"):
                    break
                preflop_actions.append(line.strip())
            self.preflop_actions = tuple(preflop_actions) if preflop_actions else None
        except Exception as e:
            logging.error(f"Error parsing preflop actions: {e}")
            self.preflop_actions = None

    def _parse_flop(self):
        """
        Parses the flop section.
        """
        try:
            if "FLOP" not in self.sections:
                self.flop = None
                return
            flop_section = self.sections["FLOP"]
            self.flop = _Street(flop_section.strip().split('\n'))
        except Exception as e:
            logging.error(f"Error parsing flop section: {e}")
            self.flop = None

    def _parse_street(self, street):
        """
        Parses a given street (turn or river).
        """
        try:
            street_key = street.upper()
            if street_key not in self.sections:
                setattr(self, f"{street.lower()}_actions", None)
                return
            street_section = self.sections[street_key].strip().split('\n')
            street_obj = _Street(street_section)
            setattr(self, f"{street.lower()}_actions", street_obj.actions)
        except Exception as e:
            logging.error(f"Error parsing {street} section: {e}")
            setattr(self, f"{street.lower()}_actions", None)

    def _parse_showdown(self):
        """
        Parses the showdown section.
        """
        try:
            self.show_down = "SHOW_DOWN" in self.sections
        except Exception as e:
            logging.error(f"Error parsing showdown: {e}")
            self.show_down = False

    def _parse_pot(self):
        """
        Parses the total pot and rake from the summary section.
        """
        try:
            if "SUMMARY" not in self.sections:
                self.total_pot = Decimal('0.00')
                self.rake = Decimal('0.00')
                return
            summary_section = self.sections["SUMMARY"].strip().split('\n')
            for line in summary_section:
                match = self._pot_re.match(line.strip())
                if match:
                    self.total_pot = Decimal(match.group("pot"))
                    self.rake = Decimal(match.group("rake"))
                    break
            else:
                # If no pot line is found, set to 0
                self.total_pot = Decimal('0.00')
                self.rake = Decimal('0.00')
        except Exception as e:
            logging.error(f"Error parsing pot information: {e}")
            self.total_pot = Decimal('0.00')
            self.rake = Decimal('0.00')

    def _parse_board(self):
        """
        Parses the board cards from the summary section.
        """
        try:
            if "SUMMARY" not in self.sections:
                self.board = None
                return
            summary_section = self.sections["SUMMARY"].strip().split('\n')
            for line in summary_section:
                match = self._board_re.search(line)
                if match:
                    cards = match.group(1).split()
                    self.board = tuple(Card(card) for card in cards)
                    break
            else:
                self.board = None
        except Exception as e:
            logging.error(f"Error parsing board cards: {e}")
            self.board = None

    def _parse_winners(self):
        """
        Parses the winners from the summary section.
        """
        try:
            if "SUMMARY" not in self.sections:
                self.winners = ()
                return
            summary_section = self.sections["SUMMARY"].strip().split('\n')
            winners = set()
            for line in summary_section:
                match = self._winner_re.match(line.strip())
                if match:
                    winners.add(match.group("name"))
                else:
                    match_showdown = self._showdown_re.match(line.strip())
                    if match_showdown:
                        winners.add(match_showdown.group("name"))
            self.winners = tuple(winners) if winners else ()
        except Exception as e:
            logging.error(f"Error parsing winners: {e}")
            self.winners = ()
