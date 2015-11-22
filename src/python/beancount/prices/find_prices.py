"""A library of codes create price fetching jobs from strings and files.
"""
__author__ = "Martin Blais <blais@furius.ca>"

import collections
import itertools
import logging
import re
import sys

from beancount.core import data
from beancount.core import amount
from beancount.ops import summarize


# A dated price source description.
#
# Attributes:
#   base: A commodity string, the base for the given symbol from the input file.
#     This may be null if we don't have a mapping for it.
#   quote: A commodity string, the quote currency that defines the units of the price.
#     This is also intended to be a commodity from the input file, and similarly,
#     may be null.
#   date: A datetime.date object for the date to be fetched, or None
#     with the meaning of fetching the latest price.
#   sources: A list of PriceSource instances describing where to fetch prices from.
DatedPrice = collections.namedtuple('DatedPrice', 'base quote date sources')


# A price source.
#
#   module: A Python module, the module to be called to create a price source.
#   symbol: A ticker symbol in the universe of the source.
#   invert: A boolean, true if we need to invert the currency.
PriceSource = collections.namedtuple('PriceSource', 'module symbol invert')


def format_dated_price_str(dprice):
    """Convert a dated price to a one-line printable string.

    Args:
      dprice: A DatedPrice instance.
    Returns:
      The string for a DatedPrice instance.
    """
    psstrs = ['{}({}{})'.format(psource.module.__name__,
                                '1/' if psource.invert else '',
                                psource.symbol)
              for psource in dprice.sources]
    base_quote = '{} / {}'.format(dprice.base, dprice.quote)
    return '{:>32} @ {:10} [ {} ]'.format(base_quote,
                                        dprice.date or 'latest',
                                        ','.join(psstrs))


def parse_source_map(source_map_spec):
    """Parse a source map specification string.

    Source map specifications allow the speification of multiple sources for
    multiple quote currencies and follow the following syntax:

       <currency1>:<source1>,<source2>,... <currency2>:<source1>,...

    Where a <source> itself follows:

       <module>/[^]<ticker>

    The <module> is resolved against the Python path, but first looked up under
    the package where the default price extractors lie. The presence of a '^'
    character indicates that twe should use the inverse of the rate pull from
    this source.

    For example, for prices of AAPL in USD:

       USD:google/NASDAQ:AAPL,yahoo/AAPL

    Or for the exchange rate of a currency, such as INR in USD or in CAD:

       USD:google/^CURRENCY:USDINR CAD:google/^CURRENCY:CADINR

    Args:
      source_map_spec: A string, a full source map specification to be parsed.
    Returns:
      FIXME: TODO
    Raises:
      ValueError: If an invalid pattern has been specified.
    """
    source_map = collections.defaultdict(list)
    for source_list_spec in re.split('[ ;]', source_map_spec):
        match = re.match('({}):(.*)$'.format(amount.CURRENCY_RE),
                         source_list_spec)
        if not match:
            raise ValueError('Invalid source map pattern: "{}"'.format(source_list_spec))

        currency, source_strs = match.groups()
        source_map[currency].extend(
            parse_single_source(source_str)
            for source_str in source_strs.split(','))
    return source_map


def parse_single_source(source):
    """Parse a single source string.

    Source specifications follow the syntax:

      <module>/[^]<ticker>

    The <module> is resolved against the Python path, but first looked up
    under the package where the default price extractors lie.

    Args:
      source: A single source string specification.
    Returns:
      A PriceSource tuple, or
    Raises:
      ValueError: If invalid.
    """
    match = re.match(r'([a-zA-Z]+[a-zA-Z0-9\.]+)/(\^?)([a-zA-Z0-9:_\-\.]+)', source)
    if not match:
        raise ValueError('Invalid source name: "{}"'.format(source))
    short_module_name, invert, symbol = match.groups()
    full_module_name = import_source(short_module_name)
    return PriceSource(full_module_name, symbol, bool(invert))


# The Python package where the default sources are found.
DEFAULT_SOURCE_PACKAGE = 'beancount.prices.sources'


def import_source(module_name):
    """Import the source module defined by the given name.

    The default location is handled here.

    Args:
      short_module_name: A string, the name of a Python module, which may
        be within the default package or a full name.
    Returns:
      A corresponding Python module object.
    Raises:
      ImportError: If the module cannot be imported.
    """
    default_name = '{}.{}'.format(DEFAULT_SOURCE_PACKAGE, module_name)
    try:
        __import__(default_name)
        return sys.modules[default_name]
    except ImportError:
        try:
            __import__(module_name)
            return sys.modules[module_name]
        except ImportError as exc:
            raise ImportError('Could not find price source module "{}": {}'.format(
                module_name, exc))


def find_currencies_declared(entries, date=None):
    """Return currencies declared in Commodity directives.

    If a 'price' metadata field is provided, include all the quote currencies
    there-in. Otherwise, the Commodity directive is ignored.

    Args:
      entries: A list of directives.
      date: A datetime.date instance.
    Returns:
      A list of (base, quote, list of PriceSource) currencies. The list of
      (base, quote) pairs is guaranteed to be unique.
    """
    currencies = []
    for entry in entries:
        if not isinstance(entry, data.Commodity):
            continue
        if date and entry.date >= date:
            break

        # Here we have to infer which quote currencies the commodity is for
        # (maybe down the road this should be better handled by providing a list
        # of quote currencies in the Commodity directive itself).
        #
        # First, we look for a "price" metadata field, which defines conversions
        # for various currencies. Each of these quote currencies generates a
        # pair in the output.
        source_str = entry.meta.get('price', None)
        if source_str:
            try:
                source_map = parse_source_map(source_str)
            except ValueError:
                logging.warning("Ignoring currency with invalid 'price' source: %s",
                                entry.currency)
            else:
                for quote, psources in source_map.items():
                    currencies.append((entry.currency, quote, psources))
        else:
            # If to "price" metadata is found, we inspect the "quote" metadata
            # field, which can be used to say which currency the currency is
            # quoted in.
            quote =  entry.meta.get('quote', None)
            if quote is not None:
                currencies.append((entry.currency, quote, None))
            else:
                # Finally, otherwise we simply ignore the declaration. That is,
                # a Commodity directive without any "price" nor "quote" metadata
                # would not register as a declared currency. Note: I'm not
                # entirely sure that this is the best approach yet, but going
                # with this behavior for now [blais/2015-11-22].
                logging.warning("Ignoring currency with no metadata: %s", entry.currency)
    return currencies


def find_currencies_at_cost(entries):
    """Return all currencies that were held at cost at some point.

    This returns all of them, even if not on the books at a particular point in
    time. This code does not look at account balances.

    Args:
      entries: A list of directives.
      date: A datetime.date instance.
    Returns:
      A list of (base, quote) currencies.
    """
    currencies = set()
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        for posting in entry.postings:
            lot = posting.position.lot
            if lot.cost:
                currencies.add((lot.currency, lot.cost.currency))
    return currencies


def find_currencies_converted(entries, date=None):
    """Return currencies from price conversions.

    This function looks at all price conversions that occurred until some date
    and produces a list of them. Note: This does not include Price directives,
    only postings with price conversions.

    Args:
      entries: A list of directives.
      date: A datetime.date instance.
    Returns:
      A list of (base, quote) currencies.
    """
    currencies = set()
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        if date and entry.date >= date:
            break
        for posting in entry.postings:
            lot = posting.position.lot
            price = posting.price
            if lot.cost is not None or price is None:
                continue
            currencies.add((lot.currency, price.currency))
    return currencies


def find_currencies_priced(entries, date=None):
    """Return currencies seen in Price directives.

    Args:
      entries: A list of directives.
      date: A datetime.date instance.
    Returns:
      A list of (base, quote) currencies.
    """
    currencies = set()
    for entry in entries:
        if not isinstance(entry, data.Price):
            continue
        if date and entry.date >= date:
            break
        currencies.add((entry.currency, entry.amount.currency))
    return currencies


def find_balance_currencies(entries, date=None):
    """Return currencies relevant for the given date.

    This computes the account balances as of the date, and returns the union of:
    a) The currencies held at cost, and
    b) Currency pairs from previous conversions, but only for currencies with
       non-zero balances.

    This is intended to produce the list of currencies whose prices are relevant
    at a particular date, based on previous history.

    Args:
      entries: A list of directives.
      date: A datetime.date instance.
    Returns:
      A set of (base, quote) currencies.
    """
    # Compute the balances.
    currencies = set()
    currencies_on_books = set()
    balances, _ = summarize.balance_by_account(entries, date)
    for _, balance in balances.items():
        for pos in balance:
            lot = pos.lot
            if lot.cost is not None:
                # Add currencies held at cost.
                currencies.add((lot.currency, lot.cost.currency))
            else:
                # Add regular currencies.
                currencies_on_books.add(lot.currency)

    # Create currency pairs from the currencies which are on account balances.
    # In order to figure out the the quote currencies, we use the list of price
    # conversions until this date.
    converted = (find_currencies_converted(entries, date) |
                 find_currencies_priced(entries, date))
    for cbase in currencies_on_books:
        for base_quote in converted:
            base, quote = base_quote
            if base == cbase:
                currencies.add(base_quote)

    return currencies


def log_currency_list(message, currencies):
    """Lot a list of currencies to debug output.

    Args:
      message: A message string to prepend.
      currencies: A list of (base, quote) currency pair.
    """
    for base, quote in currencies:
        cur_str = '{} / {}'.format(base, quote)
        logging.debug("{}: {:>32}".format(message, cur_str))


def get_price_jobs_at_date(entries, date=None, inactive=False, undeclared=False):
    """Get a list of prices to fetch from a stream of entries.

    The active holdings held on the given date are included.

    Args:
      filename: A string, the name of a file to process.
      date: A datetime.date instance.
    Returns:
      A list of DatedPrice instances.
    """
    # Find the list of declared currencies, and from it build a mapping for
    # tickers for each (base, quote) pair. This is the only place tickers
    # appear.
    declared_triples = find_currencies_declared(entries, date)
    currency_map = {(base, quote): psources
                  for base, quote, psources in declared_triples}

    # Compute the initial list of currencies to consider.
    if undeclared:
        # USe the full set of possible currencies.
        cur_at_cost = find_currencies_at_cost(entries)
        cur_converted = find_currencies_converted(entries, date)
        cur_priced = find_currencies_priced(entries, date)
        currencies = cur_at_cost | cur_converted | cur_priced
        log_currency_list("Currency at cost  ", cur_at_cost)
        log_currency_list("Currency converted", cur_converted)
        log_currency_list("Currency priced   ", cur_priced)
    else:
        # Use the currencies from the Commodity directives.
        currencies = set(currency_map.keys())
        log_currency_list("Currency declared ", currencies)

    # By default, restrict to only the currencies with non-zero balances at the
    # given date.
    if not inactive:
        balance_currencies = find_balance_currencies(entries, date)
        log_currency_list("Balance currencies", balance_currencies)
        currencies = currencies & balance_currencies

    # Build up the list of jobs to fetch prices for.
    jobs = []
    for base_quote in currencies:
        psources = currency_map.get(base_quote, None)
        if not psources:
            continue
        base, quote = base_quote
        jobs.append(find_prices.DatedPrice(base, quote, args.date, psources))
    return sorted(jobs)
