---
name: nse-broker
description: >-
  Order Executor for NSE. Turns an approved order group into broker calls,
  paper trading by default, never without your typed yes. Shows each group
  (entry, SL-M stop-loss, LIMIT target) leg by leg in plain words plus the
  exact JSON for the Zerodha Kite MCP place_order tool, and is READY only
  when nse-risk passed that exact group today in the last 30 minutes.
  Records your yes word for word (it must name the stock and the quantity),
  then Claude's own Needs approval card asks again. Kite has no bracket
  orders for retail, so it walks you through the three legs: entry first,
  stop and target only after the entry fills, and the cancel_order ticket
  for the other leg the moment one fills. With no Indian broker paper
  account, a local paper engine plays the group on real 5-minute bars and
  reports P&L net of charges. Builds the flat-by sell ticket, records fills
  and each trade's max loss so nse-risk can hold the daily limit. Covers
  setting up the Kite MCP server (hosted or self-hosted, keys never in
  chat). Use when the user says "place the order", "buy it", "execute",
  "send it to Zerodha", "paper trade it", "close my position", "flatten",
  "get me out", "connect Kite", or after /nse-risk passes.
argument-hint: "[group number]"
---

# nse-broker

The Order Executor is the only skill in this pack that can lead to a real
order, so it is the strictest. A group reaches the market only after three
separate gates, every time, for every group:

1. **nse-risk passed this exact group today**, matched by fingerprint (the
   sha256 of its legs), in its latest run, after every yes and every placed
   order today (any group), and the check is under 30 minutes old on the real
   clock. An older check, a PASS left over from an earlier run, a check made
   before another trade, or one run with a pinned `--now`, says RECHECK: run
   nse-risk again. A new entry is also never READY while NSE is closed or
   outside `no_entries_before` / `no_entries_after` (IST, real clock); exits
   are not held to the window.
2. **The user typed an explicit yes for this one group** in the chat, naming
   the stock and the quantity: "yes 73 DEMO".
3. **The user clicked Allow** on Claude's own permission card for each
   `place_order` call, because the order tools are set to "Needs approval".

No yes, no order. Nothing is placed without the user typing yes for that
exact order.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/ticket.py" show                     # every group, READY or not
python3 "${CLAUDE_SKILL_DIR}/ticket.py" approve --index 1 --reply "yes 73 DEMO"
python3 "${CLAUDE_SKILL_DIR}/ticket.py" result --index 1 --leg entry --status placed --order-id 251027000123
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --group DEMO-2026-10-27-1 --leg entry --price 1015.00
python3 "${CLAUDE_SKILL_DIR}/ticket.py" fill --group DEMO-2026-10-27-1 --leg target --price 1034.60
python3 "${CLAUDE_SKILL_DIR}/ticket.py" flatten --symbol DEMO --qty 73
python3 "${CLAUDE_SKILL_DIR}/ticket.py" log
python3 "${CLAUDE_SKILL_DIR}/paper.py" run --group DEMO-2026-10-27-1
python3 "${CLAUDE_SKILL_DIR}/paper.py" status
```

`ticket.py` never talks to a broker. It renders tickets, checks that a reply
is a real yes for that group, builds the flatten ticket and keeps the log in
`nse/state/broker.json`. Times are IST.

## What it reads and writes

| file | what |
| --- | --- |
| `nse/orders.json` | the proposed groups (nse-budget writes it; `flatten` replaces it) |
| `nse/state/risk.json` | nse-risk's verdicts by fingerprint; only a fresh PASS from today is READY |
| `nse/state/broker.json` | every yes, no, placed leg, fill and paper P&L, with each entry's max loss (nse-risk's measured one) |
| `nse/state/paper-replay.json` | `paper.py run --approved-at` replays only; never counted by nse-risk |
| `nse/portfolio.json` | cash and shares, updated by `ticket.py fill` (live fills only) |
| `nse/config.json` | `"broker"`: `"paper"` (default) or `"kite"`, and `"paper"` to match (true with paper, false with kite); anything else is an error |

## Paper by default

No Indian broker offers a paper (demo) account, Zerodha included. So with
`"broker": "paper"` (the default) `approve` records the yes and hands the
group to `paper.py run --group G`, which never sends anything anywhere:

- the entry fills at the OPEN of the first 5-minute bar that starts at or
  after the approval time;
- then bar by bar: the stop fills when a bar's low reaches the trigger (at
  the trigger, or at the bar's open if it opened below it: a gap); else the
  target fills when a bar's high reaches the price (at the price, or the
  open if it opened above); both inside one bar counts as the stop (the
  conservative call);
- nothing hit: square-off at the close of the bar that ends at `flat_by`
  (15:10 is the 15:05 to 15:10 bar);
- P&L = (exit - entry) x qty minus the round-trip charges (brokerage, STT,
  exchange, SEBI, stamp, GST).

The fills go to `nse/state/broker.json` in the same shape as live ones, so
nse-risk counts paper trades, orders and max loss like real ones (the max
loss is measured from the entry fill: qty x (fill - stop) plus the round-trip
charges). `run` needs the recorded yes and plays from its time; it refuses a
new entry outside NSE hours or the entry window, and refuses to run at all
with `"broker": "kite"`.

`paper.py run --approved-at "YYYY-MM-DD HH:MM"` is a REPLAY: a what-if on a
past session that writes `nse/state/paper-replay.json` only, never
`broker.json`, and prints REPLAY. It is not a trade and nse-risk never counts
it. `approve`, `decline`, `result` and `fill` always stamp the real clock and
refuse `--now`. Approvals count toward nse-risk's limits until declined or placed. Run during
the day, the position stays open until the bars reach a fill or flat_by:
`paper.py status` lists it, and the same `run` command finishes it later.
`--offline DIR` reads saved bars and never touches the network. A paper fill
is a model: real fills can be worse.

## Setup: Kite MCP (only for real money)

Zerodha's own server is github.com/zerodha/kite-mcp-server (MIT), checked on
2026-10-28 (commit 5e044dc). Both variants are in
`claude_desktop_config.example.json`; keep one.

- **Hosted**: `https://mcp.kite.trade/mcp`, through `npx mcp-remote`. No install
  and no API key; the `login` tool gives a Kite login link to open in the
  browser. Its README says the hosted server leaves out `place_order`,
  `modify_order` and `cancel_order`: it is read-only for orders. It reads
  quotes, orders and positions, but it cannot place, modify or cancel one.
- **Self-hosted** (the only way to place live orders): build `zerodha/kite-mcp-server`
  (`go build -o kite-mcp-server`), create a Kite Connect app for an API key
  and secret, and put them in the `env` block with `"APP_MODE": "stdio"`.

**Keys never go in the chat.** They live in environment variables or the
Claude Desktop config file. Never ask for them, never read that file back
into the conversation, and if the user pastes a key anyway, tell them to
delete the message and regenerate it in the Kite Connect developer console.

After a restart: Customize > Connectors > kite, and set `place_order`,
`modify_order` and `cancel_order` (and the GTT tools) to **Needs approval**.
Never Always allow. Read-only tools (`get_orders`, `get_positions`,
`get_quotes`, `get_ltp`, `get_order_history`, `get_trades`) can be Always
allow. In Claude Code keep them on ask: `"permissions": {"ask":
["mcp__kite__place_order", "mcp__kite__modify_order", "mcp__kite__cancel_order"]}`.
Then set both `"broker": "kite"` and `"paper": false` in `nse/config.json`.
That is the user's own step. The two keys must agree: `"broker": "kite"` with
`"paper": true` (or `"broker": "paper"` with `"paper": false`) is a config
error, and nse-budget, nse-risk, `ticket.py` and `paper.py` all stop on it.

**Verified tools.** Orders: `place_order`, `modify_order`, `cancel_order`,
`get_orders`, `get_trades`, `get_order_history`, `get_order_trades`. GTT:
`get_gtts`, `place_gtt_order`, `modify_gtt_order`, `delete_gtt_order`. Data and
account: `login`, `get_quotes`, `get_ltp`, `get_ohlc`, `get_historical_data`,
`search_instruments`, `get_profile`, `get_margins`, `get_holdings`,
`get_positions`, `get_mf_holdings`.

`place_order` takes `variety` (regular, co, amo, iceberg, auction),
`exchange`, `tradingsymbol`, `transaction_type` (BUY, SELL), `quantity`
(number), `product` (CNC, NRML, MIS, MTF), `order_type` (MARKET, LIMIT, SL,
SL-M), and optionally `price`, `trigger_price` (numbers), `validity` (DAY,
IOC, TTL), `validity_ttl`, `disclosed_quantity`, `iceberg_legs`,
`iceberg_quantity`, `tag`. `modify_order` takes `variety`, `order_id`,
`order_type` and optionally `quantity`, `price`, `trigger_price`, `validity`,
`disclosed_quantity`. `cancel_order` takes `variety` and `order_id`. The leg
keys nse-budget writes are these names already; `ticket.py` only drops our
`role` key, so the JSON it prints is the call.

## Steps: one group, three legs

1. `ticket.py show`. Only READY groups go to the user. NOT READY, RECHECK or
   BLOCKED goes back to `/nse-risk`.
2. Show ONE group and ask: "Place this group? Reply yes with the quantity
   and the stock (yes 73 DEMO), or no." One group per question. Never offer
   "yes to all".
3. `ticket.py approve --index N --reply "<the user's reply, word for word>"`.
   "Not recorded" means it was not an explicit yes for this group (a plain
   "yes", "sure", "yes to all", a different stock or amount, or a yes that
   fits two groups). Ask again. Never turn a reply into a yes yourself.
4. Paper: run the `paper.py run` line it prints and show the result.
5. Kite: Kite has no bracket orders for retail, so the legs go in one by one:
   1. `place_order` with the entry JSON (the user clicks Allow), then
      `ticket.py result --index N --leg entry --status placed --order-id ID`.
   2. When `get_orders` shows the entry COMPLETE: `ticket.py fill --group G
      --leg entry --price P`. Only now place the stop-loss and the target: it
      prints both `place_order` calls. Record each with `result`.
   3. When the stop or the target fills: `ticket.py fill --group G --leg stop
      --price P` (or `--leg target`). It prints the `cancel_order` call for the
      other leg with its order id. Cancel it at once, or it stays open and can
      sell again into a short position.
6. Kite only: by `flat_by` (15:10 IST, before the broker's MIS auto
   square-off around 15:20), anything still open is closed: cancel the open
   stop and target with `cancel_order`, `ticket.py flatten --symbol S --qty Q`,
   `/nse-risk`, then `show` and the user's yes as above. Paper needs no
   flatten: `paper.py run` squares off at `flat_by` itself.

## What it never does

- Never places an order without all three gates, and never two groups on one yes.
- Never places the stop or the target before the entry has filled.
- Never places orders from a scheduled or background task: nobody is there to say yes.
- Never asks for, reads, stores or repeats API keys, the Kite login or passwords.
- Never switches `"broker"` from paper to kite (or `"paper"` to false). The user does that themselves.
- Never promises a fill price: market and SL-M orders fill at what the market gives.

## Safety

- Not financial advice. Education and tooling; a PASS is a rules check, not a recommendation.
- Paper trading by default. Going live is the user's own decision, in their own config.
- Nothing is placed unless nse-risk passed that exact group, the user typed yes naming it, and the
  user approved each call on Claude's card. Never set an order tool to "Always allow".
- No F&O, leverage or short selling unless the config allows it and the user confirms.
