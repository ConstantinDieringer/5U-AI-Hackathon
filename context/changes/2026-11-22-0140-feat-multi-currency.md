Bids, tariff sheets and portal rates accept any currency (JPY, GBP, CHF, CNY ... and symbols); converted to EUR only with `FX_EUR_RATES` + `FX_VALID_UNTIL` (legacy `USD_EUR_RATE` still works), else skipped. Customer quotes stay EUR.
Not verified: live 5U foreign-currency bids, portal field name for non-USD rates.
Pending decision: set FX_EUR_RATES in Railway (source of rates) and whether customers may get quotes in their currency.
