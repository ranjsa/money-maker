# nse-orderbook backtest, 2024-11-01 to 2026-10-28

Rules: monthly signal on the last trading day; orders in the last 90 days at least 10% of annual revenue and at least 1.25x the company's usual pace; revenue growing at least 10%; close at least Rs 50 and at least Rs 10 cr traded a day; top 10; each pick held 3 months; equal weight; enter at the next open.

| | Strategy | Nifty 50 |
|---|---|---|
| Start capital | Rs 1,000,000 | |
| End value | Rs 1,068,341 | |
| Total return | 6.8% | -6.6% |
| CAGR | 3.4% | -3.4% |
| Max drawdown | -34.0% | -15.6% |
| Months beating Nifty | 10 of 23 | |
| Trades | 415 | |
| Charges paid | Rs 15,472 | |
| Months with no pick | 0 | |

Order values found for 1619 of 2027 filings (189 from text, 1386 from PDF, 44 bands).

## Picks by month

- 2024-11-29: ASHOKA, PREMIERENE, AZAD, POLYCAB, RAILTEL, AVANTEL, GRSE, POWERMECH, TDPOWERSYS
- 2024-12-31: KERNEX, ASHOKA, PREMIERENE, AZAD, DCXINDIA, POLYCAB, RAILTEL, GRSE, MAZDOCK, TDPOWERSYS
- 2025-01-31: KERNEX, AZAD, PREMIERENE, EMSLIMITED, DCXINDIA, POLYCAB, POWERMECH, MAZDOCK, TRITURBINE, NEWGEN
- 2025-02-28: AZAD, JBMA, EMSLIMITED, PREMIERENE, HGINFRA, RAILTEL, POWERMECH, MAZDOCK, TRITURBINE, HBLENGINE
- 2025-03-28: AZAD, JBMA, EMSLIMITED, PREMIERENE, POWERMECH, RAILTEL, ZENTEC, IRCON, NCC, DBL
- 2025-04-30: JBMA, POWERMECH, RAILTEL, ZENTEC, IRCON, INTERARCH, NCC, EMSLIMITED, GVT&D, HBLENGINE
- 2025-05-30: BDL, POWERMECH, TARIL, INTERARCH, ZENTEC, GVT&D, GOLDIAM
- 2025-06-30: INTERARCH, NIBE, TEXRAIL, GOLDIAM, AVANTEL
- 2025-07-31: DIACABS, WABAG, MAPMYINDIA, EIEL, NIBE, PREMEXPLN, SERVOTECH, TEXRAIL, POWERMECH, KPIL
- 2025-08-29: DIACABS, WABAG, BLS, PREMEXPLN, E2E, MAPMYINDIA, EIEL, RAILTEL, TEXRAIL, ASTRAMICRO
- 2025-09-30: E2E, NETWEB, DIACABS, RAILTEL, WABAG, BLS, WAAREERTL, PREMEXPLN, MANINDS, EIEL
- 2025-10-31: E2E, NETWEB, GARUDA, PREMEXPLN, MTARTECH, WAAREERTL, BLS, RAILTEL, SAATVIKGL, MANINDS
- 2025-11-28: GARUDA, KMEW, VIKRAN, E2E, NETWEB, MTARTECH, PREMEXPLN, WAAREERTL, SAATVIKGL, BHEL
- 2025-12-31: GARUDA, KMEW, VIKRAN, MTARTECH, SAATVIKGL, LT, PREMEXPLN, DIACABS, BDL, GKENERGY
- 2026-01-30: KERNEX, GARUDA, VIKRAN, LT, KMEW, BDL, MTARTECH, WAAREERTL, DIACABS, ZENTEC
- 2026-02-27: KERNEX, LT, VIKRAN, OLECTRA, WAAREERTL, QPOWER, DIACABS, SHAILY, AVANTEL, MTARTECH
- 2026-03-30: KERNEX, AVANTEL, LT, EIEL, QPOWER, DATAPATTNS, OLECTRA, WAAREERTL, BHEL, POWERMECH
- 2026-04-30: AVANTEL, EIEL, QPOWER, DATAPATTNS, ARIS, OLECTRA, RAILTEL, SHAILY, OMNI, BHEL
- 2026-05-29: MTARTECH, AVANTEL, EIEL, ARIS, DATAPATTNS, TEXRAIL, OMNI, PREMEXPLN, SKIPPER, QPOWER
- 2026-06-30: MTARTECH, STLTECH, MARINE, DSSL, DEEDEV, HFCL, BHEL, INTERARCH, TARIL, SAIPARENT
- 2026-07-31: MTARTECH, WELCORP, STLTECH, ASTRAMICRO, MARINE, DSSL, WABAG, ADVAIT, BHEL, SKIPPER
- 2026-08-31: E2E, WELCORP, ASTRAMICRO, MARINE, ADVAIT, WABAG, STLTECH, DATAPATTNS, BHEL, KRISHNADEF
- 2026-09-30: WELCORP, E2E, ASTRAMICRO, MARINE, DIACABS, STLTECH, DATAPATTNS, KPIL, GOLDIAM, JNPR
- 2026-10-28: E2E, STLTECH, MARINE, WELCORP, KPIL, KMEW, DATAPATTNS, KNACK, HEGAM, JNPR

## Equity curve

```
   1,175,884 |                                                            * 
   1,139,938 |                                                         * *  
   1,103,992 |                                                  **    * *   
   1,068,046 |                                                    * **      
   1,032,100 |   ***                                          **   *       *
     996,154 |***               *                           **              
     960,208 |                 * ****    * ***                              
     924,262 |      **               * ** *   *            *                
     888,316 |                *       *        *                            
     852,371 |        *                           *                         
     816,425 |         *   ***                  ** * ***  *                 
     780,479 |            *                             **                  
     744,533 |          **                          *                       
              2024-11-01 ... 2026-10-28 (strategy)
```

Read this before trusting the number:
  - Past results, simulated. Not financial advice, and not a buy list.
  - Universe = companies that filed orders and that Yahoo still has prices for (survivorship bias).
  - Order values are read from PDFs by pattern; some are wrong or missing (coverage above).
  - Revenue is assumed known 60 days after each fiscal year ends; Yahoo's figures can be restated.
  - Fills at the next day's open with no slippage; delivery charges included, taxes on gains not.
  - Two years and a few dozen trades is a small sample: luck can explain all of it.


## Variants (same data, run 2026-10-30)

| Variant | Total return | Max drawdown |
|---|---|---|
| Base (above): revenue growth >= 10%, hold 3 months | 6.8% | -34.0% |
| No revenue-growth filter | 2.7% | -33.4% |
| Hold 6 months | 3.7% | -34.4% |
| Nifty 50 buy and hold | -6.6% | -15.6% |
