"""Checksum-valid TRON addresses used by the simulation and tests.

The look-alikes were produced with a small vanity search (no private keys
exist or were generated - only the Base58Check encoding was searched), the
same technique attackers use with GPUs.  ``POISON`` shares the first 5
characters after ``T`` (``Legit``) and the last 4 (``Wr2c``) with ``LEGIT``:

    LEGIT        TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c
    POISON       TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c
"""

from app.utils.address import address_from_seed

VICTIM = "TVictimxw466QvBptjwLHfEf3ekBSMZkyw"
VICTIM_2 = "TVictimGeb9QfXhe5f1vXHcFDnXbuHFN7p"
LEGIT = "TLegit9dwtq5H8YqVXiRsE7Y2zvRTSWr2c"
POISON = "TLegitVxcwrWVZweDCtZXhgsJ8xpBQWr2c"  # prefix + suffix look-alike of LEGIT
POISON_2 = "TLegitXJjajPAR1QXJitWmxfLANVvCWr2c"  # second look-alike of LEGIT
POISON_SHORT = "TLegZBTmThKXvEsXtZCZaGs16kwn6PWr2c"  # first 4 chars incl. T + last 4 match LEGIT (common real pattern)
PREFIX_ONLY = "TLegittHMDGDu8KXcgGAchjZ1wJ2xT6qZS"  # shares only the prefix with LEGIT
SUFFIX_ONLY = "TQrHKkktwyCiFjAxFioC95bwNfsvUzWr2c"  # shares only the suffix with LEGIT
LEGIT_B = "TExchGL7oSXA652ScKtq4FabQJrCv84FSx"
POISON_B = "TExch7gmqiaGJpHKo5jVLWkxTuLm4H4FSx"  # look-alike of LEGIT_B

FUNDING_SOURCE = address_from_seed("sim:funding-source")
OTHER_RECIPIENTS = [address_from_seed(f"sim:other-recipient-{i}") for i in range(4)]
DUSTED_WALLETS = [address_from_seed(f"sim:dusted-{i}") for i in range(6)]
HOPS = [address_from_seed(f"sim:hop-{i}") for i in range(2, 5)]
EXCHANGE = address_from_seed("sim:exchange-deposit")
