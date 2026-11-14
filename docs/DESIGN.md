# CartMates: design

Forked from Queuify (single-household v1, github.com/angqd/Queuify @ 5c1d8db) on 2026-11-09. Sections 1-16 describe that
v1 design, which CartMates starts from. The multi-household design (v2) is section 17; the agreed decisions are in
reference/MULTI_HOUSEHOLD_DECISIONS.md (gitignored).
Inputs: the Instamart MCP POC (notes copied to `reference/SWIGGY_MCP_FINDINGS.md`) and the design Q&A of 2026-10-28.

## 1. What it is

A Telegram bot that one household of roommates shares, inside their group chat. Roommates ask for
groceries in natural language ("@cartmates need eggs and the usual milk"). The bot keeps a shared
**queue**: a synthetic cart that lives in the bot, not in Swiggy. When someone wants to order, the bot
builds the real Instamart cart, shows the bill, and places a **cash-on-delivery** order after a Confirm tap.
It records who asked for what, so the person who paid knows who owes them what.

- **v1:** one Docker container on this Mac. One container = one bot token = one household = one Telegram group.
- **v2:** several households in one container, still on this Mac (section 17).
- **v3:** the same container on an always-on VPS, and friends' households. v3 starts only after Swiggy grants
  production access.

## 2. Decisions

| Area | Decision |
|---|---|
| Chat surface | The household's group chat only. DMs are a stretch goal. |
| When the bot responds | An @mention, a reply to the bot, or a `/command`. Other chatter is ignored. |
| Household identity | Exactly one group per deployment. Group members are roommates. |
| Processing | One serial lane per household: messages and button taps are handled one at a time, in order. |
| Swiggy account | One account (the host's), logged in through the bot. |
| Who can order | Any roommate. Whoever taps Confirm is the orderer and pays cash at the door. |
| Payment | Cash on delivery or UPI, chosen per order on the bill card (section 7.3). UPI was added on 2026-11-05 after Swiggy refused COD for the account ("User is not eligible for COD order"). It has a ~60 s payment window, so the bot hides the Pay button once the window closes. |
| Fees | The orderer pays all fees. They are not split. |
| Who owes what | A per-order record of each person's item amounts. No running balances, no /settle. |
| Shared items | An item can have several owners. Its cost is split equally among them. |
| Ambiguous items | Always ask: show the candidate variants as buttons. |
| "I want X now" | Ask "just yours or the whole queue?", showing both totals. |
| Free-delivery minimum | A config number, plus a live check against the real Swiggy cart when the queue looks big enough. |
| Out of stock at Confirm | Drop the item from this order, keep it queued, show the new bill, ask for Confirm again. |
| Editing the queue | Anyone can edit or remove anything. Every change is announced in the group, and when someone changes a line they don't own, the bot @mentions its other owners on the reply (a `tg://user` link, so it works without a username). |
| Item expiry | None in v1. `/list` shows how long each item has waited. |
| Storage | SQLite file in a Docker volume. |
| LLM | OpenAI with tool calling. The model is set in config. |
| Agent implementation | The `openai` Python SDK (Responses API) and a tool-calling loop we write ourselves. No agent framework. |
| Runtime | Python 3.14, both locally (`.venv`) and in Docker (`python:3.14-slim`). |
| Login | Paste-back OAuth (section 8). |
| Codebase | Fresh build. Borrow ideas from the old prototype in `app/` and code from `instamart_poc/`. |

## 3. The core idea: multi-turn conversation, one-shot execution

The conversation can take as many turns as it needs: clarifying a variant, logging in, choosing
"just mine or everything". During those turns the LLM can only **read** Swiggy (search, go-to items) and
**edit the bot's own queue**.

Nothing with side effects outside the bot happens until a human taps **Confirm** on a bill card. That tap
starts the **executor**: one fixed piece of plain code that builds the Swiggy cart, checks the bill and places
the order. The LLM cannot call `update_cart`, `clear_cart`, `checkout` or `confirm_order`. Those tools are not
in its tool list.

```
 group message ──► gate ──► household lane (FIFO, one at a time)
                               │
               ┌───────────────┴───────────────┐
               ▼                               ▼
        agent turn (LLM)                 button tap
   tools: search, queue edits,      (variant pick, just-mine/all,
   list, history, propose_order      Confirm, Cancel)
               │                               │
               ▼                               ▼
       replies + bill card ◄────────── executor (plain code)
                                        quote → checkout(Cash) → record
```

## 4. Components

| Component | Responsibility |
|---|---|
| **Telegram adapter** | Receives updates (long polling in v1). Applies the gate. Renders cards and buttons. Sends replies. |
| **Gate** | Passes a message only if it @mentions the bot, replies to a bot message, or is a `/command`. Button taps always pass. |
| **Household lane** | One asyncio queue plus one worker. Every message and tap goes through it, so state changes never race. Each turn sees the result of the turn before. |
| **Agent** | OpenAI tool-calling loop for one turn: system prompt + recent group memory + a queue snapshot + the new message. At most ~6 tool calls per turn. |
| **Agent tools** | Thin wrappers over the domain services (section 6). |
| **Domain services** | Queue, pending clarifications, orders, splits. Pure logic over SQLite. All rules live here, not in the prompt. |
| **Executor** | The order state machine (section 7). The only code that writes to the Swiggy cart or checks out. |
| **Swiggy client** | One MCP session to `https://mcp.swiggy.com/im`. Parses `structuredContent` and ignores the display-instruction text. Rate-limit backoff. |
| **Auth manager** | Token storage, refresh, logged-out detection, paste-back login. Parks a turn while login is pending. |
| **Store** | SQLite, schema migrations, one connection owned by the lane. |

## 5. Main flows

### 5.1 Add an item
1. "@cartmates 2 amul taaza 1L for me and rohan"
2. The agent calls `search_catalog("amul taaza 1L")`. The bot's own address is used; the LLM never chooses one.
3. One clear match: `add_to_queue(spinId, skuId, qty=2, owners=[me, rohan])`. The bot replies with the item,
   its estimated price and the new queue total.
4. Several variants: the agent calls `ask_variant(...)`. The bot posts the options as buttons. The pending
   question is stored, keyed by the user who asked. A person may have several open at once; each button
   carries its own question id. Only that user's tap resolves it. Other people can keep
   talking meanwhile; their messages are not taken as answers.
   The agent passes 2-4 refs; the code adds the other in-stock variants of the first ref's search (picks first,
   Swiggy's order, at most 24). Buttons show 4 options a page, then "More options ▸" (callback `v:<id>:m<page>`,
   edits the same message with no model call; after the last page it wraps to the first) and "None of these".
   Option callbacks index the full list. Labels are built in code: `<part that differs> · <size> · ₹<price>`.
5. The threshold check runs (5.3).

### 5.1a Several items in one message (B2)
One turn handles every item; asking a question does not end it.
1. The agent searches each item (several tool calls per round are fine), calls `add_to_queue` for each item with one
   clear match, and calls `ask_variant` once per unclear item. There is no limit on the number of questions; each is
   its own message with its own buttons and pending row, and its options come from the search that holds its first ref.
2. Items with no results are reported, with an offer to search another term. Nothing is dropped silently.
3. The turn ends with one short summary reply, e.g. "Added garlic (#23) and Tata salt (#24). Pick pasta and cheese
   above. I couldn't find tomato puree; want me to search for 'tomato paste'?"
4. Turn ends when: the model answers with no tool calls; `propose_order` posts its card or scope question (no
   reply after it); or a cap is hit. Caps: `MAX_ROUNDS = 10` model rounds and `MAX_TOOL_CALLS = 40` tool calls per
   turn. A cap reply is "Done so far: <actions>. I ran out of steps before finishing, so ask me again for the rest."
   (the old apology only when nothing was done).
5. If the model ends with no text, code builds the reply from the actions: "Done: added #1 …; added #2 …. Pick an
   option above." (or just "Pick an option above." when only questions were asked). An empty message is never sent.
6. The log outcome is `asked` when the turn only asked questions, `replied` otherwise (`proposed`, `too_long` and
   `llm_down` as before). Each question is an `asked <name> "<question>" (question #N)` action line in `chat_memory`.
7. A question that fails to send raises as in B1: the earlier adds and earlier questions stay, and the "Done so far"
   reply lists them; the failed question leaves no pending row.

The queue stores the original wording too, so an item can be re-resolved later if its variant disappears.

### 5.2 Order now
- "@cartmates order now" calls `propose_order(scope="all")`.
- "@cartmates I want eggs now": the agent adds the eggs, then calls `propose_order(scope="ask", focus=item)`. The bot
  posts two buttons, each with its estimated total: *Just your eggs* and *Whole queue*.
- "Just mine" means only that user's items. Everything else stays queued.
- The proposal produces a **bill card** using a live quote (section 7, step 2): items, owners, Swiggy's
  bill breakdown and the total to pay. It has **Confirm (COD)** and **Cancel** buttons.

### 5.3 Free-delivery nudge
1. After any queue change, compute the estimated subtotal from stored prices.
2. If it is at least `FREE_DELIVERY_MIN`, the queue changed since the last check, and no proposal is open:
   do a live quote. This calls `update_cart`, which **overwrites the host's own Instamart cart**. This is accepted.
3. If the live bill shows delivery as FREE, post "Free delivery reached (₹X). Order now?" with a proposal card.
4. After a Cancel or no answer, don't nudge again until the queue grows.

**M5 build details (2026-11-05):**
- **Where it runs:** at the end of any lane event that changed the queue (an agent turn, a variant tap,
  a removal in chat), inside the same Swiggy turn. It runs only if the event changed `queue_version`.
- **Growth rule:** `kv.nudge_base` holds the estimated total at the last check.
  - A check happens only if the estimated total is at least `FREE_DELIVERY_MIN` **and** above `nudge_base`.
  - After every check, `nudge_base` is set to the current total.
  - If the total ever drops below `nudge_base` (a removal, or an order placed), `nudge_base` drops with it.
    That way growth is measured again from the lower total.
- **Skipped when:** a proposal is open, an order is `unknown` or in flight, the bot is logged out, or no address is set.
  Every skip is silent. A logged-out bot never starts a login just to nudge.
- **The check:**
  - Build a scope-`all` quote exactly as `/order` does, but post a card only if Swiggy's bill line containing
    "Delivery" reads `FREE`. The card is headed "🎉 Free delivery reached (₹X). Order now?" and keeps the normal
    pay and cancel buttons.
  - If delivery isn't free, post nothing and log one INFO line with the delivery fee.
  - Nudge errors (Swiggy unreachable, a cart error) are logged and never shown to the group or allowed to break
    the turn that triggered them.
- Each check overwrites the host's Swiggy cart. This is accepted (section 15).

**Small fixes added with M5:**
- **`/track [id]`** (hidden command; the agent's `track_order` tool shares the same code, `orders.track`):
  - Shows the latest placed order (or the order given) using `get_delivery_status(orderId, addressId)`, with the
    order's own stored address.
  - Wording: "Swiggy #X: arriving in ~12 mins (by 19:52 IST)", "Delivered ✅" or "Cancelled". If the order wasn't
    placed, say so.
  - Automatic tracking updates are left for later.
- **Order results in chat memory:** placed, failed, unknown, cancelled and resolved each add one `[action]` line
  (for example "order #6 placed by Angad: Swiggy #…, ₹148 UPI"). The agent can then answer "did it go through?".
- **Clearer item labels:** the variant goes in parentheses and the quantity is always "×N". For example
  "Coca-Cola Diet Coke Can 330ml (330 ml x 2) ×1". This applies to the snapshot, `/list`, cards and announcements.

### 5.4 Confirm
Whoever taps Confirm becomes the orderer and payer. The executor runs (section 7). The group sees the
result: Swiggy's order ID, the ETA and the per-person amounts owed to the orderer.

### 5.5 History
`/history` (hidden command; the agent's `order_history` tool shares the summary) lists recent orders: date, orderer, total and status. `/history <id>` shows items, owners and who owes
the orderer how much. Delivery status comes from `get_delivery_status` on request.

## 6. Agent tools (LLM-visible)

| Tool | Effect |
|---|---|
| `search_catalog(query)` | Swiggy `search_products` at the pinned address. Returns every product Swiggy lists (up to 20; repeated spin ids merged) as one line each, with its variants inline and OUT marked: `p3 Bru Instant Coffee Pouch: v1 50 g ₹119 · v2 50 g ×2 ₹238 OUT`. No paging. |
| `go_to_items()` | Swiggy `your_go_to_items`, for "the usual". |
| `add_to_queue(spinId, skuId, qty, owners, original_text)` | Adds or merges a queue item. Checks maxQuantity. Announces the change. |
| `ask_variant(user, options)` | Posts variant buttons (with More options) and creates a pending clarification and records an `asked …` action line. Does not end the turn: the agent goes on with the other items and writes one summary. |
| `close_question(question_id)` | Closes an open variant question that belongs to the sender: deletes the pending row, edits its message to "Answered in chat" with no buttons, records an action line. Another member's question returns `only <asker> can close that`. Use after the sender answers in text and `add_to_queue` has run. Doesn't end the turn. |
| `update_queue_item(number, qty?, owners?)` | Edits an item. Announced, with who changed it. |
| `remove_from_queue(number)` | Removes an item. Announced. |
| `list_queue()` | Items, owners, estimated prices, age and total vs the free-delivery minimum. |
| `propose_order(scope, focus?)` | Builds a bill card, or the just-mine/all choice. Never places an order. |
| `track_order(order_id?)` | Read-only. Live ETA and status text of a placed order (default: the latest placed one); the same code as `/track` (`orders.track`). Changes nothing. |
| `order_history(count?)` | Read-only. The `/history` summary: recent orders with date, orderer, total and status. Default 5, max 10. Changes nothing. |

There is deliberately no tool for the delivery address or the login: those stay command-only (section 10).

Prompt rules are for tone and intent only. Every hard rule is enforced in code: quantity caps, valid
owners, never ordering without Confirm, and the pinned address.

**How the loop is built.** We use the `openai` SDK directly and write our own loop, rather than using the
OpenAI Agents SDK or LangChain. The agent is deliberately small: one turn, at most 6 tool calls, nothing with
side effects. A framework would mostly add things we don't need (handoffs, hosted sessions), and its memory
would duplicate the SQLite snapshot. Three pieces:

- `agent/llm.py`: an `LLM` interface with one method ("given messages and tools, return text or tool calls").
  `OpenAILLM` implements it with the Responses API; `ScriptedLLM` returns fixed tool calls for tests.
- `agent/tools.py`: each tool is a JSON schema plus a Python function that calls a domain service. Arguments are
  validated in code; model output is treated as untrusted input.
- `agent/loop.py`: build the context, then repeat model → tools → model until the model answers or 6 rounds pass.
  Returns the reply text plus any buttons to render.

OpenAI's server-side conversation state is not used. Each turn's context is rebuilt from SQLite, so memory lives
in one place.

**Prompt structure (rebuilt every turn, nothing kept server-side):**
1. Static system prompt: role, tone, rules, tool guidance. Kept first and unchanged so OpenAI's automatic
   prompt caching applies.
2. State snapshot from SQLite: time, members (name + id), queue lines with their #slot numbers, owners and estimates,
   total vs the free-delivery minimum, open proposal, an "Open questions" block with every member's open variant questions, login state, address label.
   This is the source of truth; if it disagrees with the transcript, the snapshot wins.
3. Recent transcript (see memory below), each line tagged with the speaker.
4. The new message, tagged with the speaker's name and id. A reply to a bot message adds who the bot message was
   for and what it was: `Shounak (id 9), replying to the bot's message to Angad ("Diwali gift options…"; open question #19): what about sukoon`.
   Unknown parts are left out; a message we have no record of is `replying to an earlier bot message`.

**Harness rules:** tool arguments are validated in code. `add_to_queue` accepts only products returned by a
search made in the same turn, so a made-up id can't enter the queue. Owner names are resolved to member ids in
code. Button taps are resolved by code without calling the model. Limits per turn: 6 tool rounds, an output cap
and a timeout; on hitting one the bot apologises and the queue is never left half-edited. Each tool call
(name + arguments, not the full result) is written to `events`.

**Memory:** `chat_memory` stores, per turn, the user's message, the bot's reply and one-line action summaries
("added #12 Amul Taaza 1L ×2 for Priya, Rohan"). Raw tool results are never stored. The model sees the last
20 exchanges from the past 12 hours.

**Threading (B6):** a group chat interleaves several conversations, so the model is told who each line is for.
- Migration 006: `chat_memory.to_user_id` and `chat_memory.message_id` (the addressee and the Telegram id of an
  assistant row), and `pending.message_id` (the message that carries a variant question's buttons).
  The agent loop stores both on the final reply; `ask_variant` stores the id on the pending row and on its row.
- Assistant rows render as `[assistant → Angad] Which instant coffee…`; rows without `to_user_id` render as `[assistant]`.
- The adapter sets `Message.reply_to_message_id` and `reply_to_bot`. For a reply to our bot the agent looks the id up
  in `chat_memory` (text, capped at 120 characters, and the addressee), `pending.message_id` (the open question)
  and `orders.card_message_id` (the order). Text with "http" or a login paste is never quoted.
- The snapshot lists every member's open questions, oldest first, newest 10 at most, the sender's own marked:
  `#19 Angad, 4 min ago: Which instant coffee? Options: Continental Xtra Instant Coffee Pouch 50 g; Bru …; (+12 more)`.
  Only the agent's picks are listed (the first 4 for rows from before `picked`).
- Only the asker can answer a question: by tap, or in text followed by `close_question`.
- Questions also leave an action line when they end without an item: `Angad: none of the options for "…"` on
  *None of these*, and `Angad's question "…" expired` on expiry (once, whichever of the lazy and hourly paths sees it first). No preference learning in v1: "the usual" uses Swiggy's go-to items.

## 7. Executor: order state machine

```
proposed ──Confirm──► claimed ──► quoting ──► checking_out ──► placed
    │                    │           │               │
    └─Cancel─► cancelled │      drift / out of stock └─ error ─► verify ─► placed | unknown
                         │           ▼
                         │    re-proposed (new bill card)
                         └─ second tap: no-op (atomic claim)
```

1. **Claim:** `UPDATE orders SET status='claimed' WHERE id=? AND status='proposed'`. Zero rows changed means
   someone already confirmed, so do nothing. This stops a double tap from ordering twice.
2. **Quote:** `update_cart` (whole list, pinned address), then `get_cart`. Ignore ₹0 items that Swiggy adds
   on its own (promo flyers). Compare with the bill card:
   - An item is out of stock or its quantity is capped: drop it, leave it queued, and re-propose.
   - The total rose by more than `PRICE_TOLERANCE` (default 5%): re-propose.
3. **Checkout:** `checkout(addressId, paymentMethod="Cash")`. Success is `status: CONFIRMED` plus an `orderId`.
4. **Verify on error:** for a network error, timeout or unclear response, **never retry**. Call `get_orders`
   and look for a new order created after the claim time with matching items.
   - Found: treat it as placed.
   - Not found: mark the order `unknown`, block new orders, and ask a human to check the Swiggy app and run `/resolve`.
5. **Record:** order row, item snapshot with owners and actual prices, per-person shares and the Swiggy order ID.
   The covered queue lines are marked `ordered` (section 9.1).

A restart while an order is `claimed`, `quoting` or `checking_out` resolves to `unknown` on boot, and
the same verify step runs.

### 7.1 M4 build details

Decisions taken when M4 started (2026-11-05). Response shapes come from the POC recordings in
`instamart_poc/results/calls/`.

**Proposal (`domain/orders.py`, plain code).**
- Entry points: the agent tool `propose_order(scope)` with scope `all`, `mine` or `ask`; `/order`, `/order mine` and `/order all`. A bare `/order` asks the same
  everything-or-just-mine question as scope `ask` when the queued lines belong to more than one person, and proposes
  scope `all` directly when they belong to one. Unlike `ask_variant`, the agent tool ends the turn, because the bot posts the card itself.
- `ask` posts two buttons from stored estimates: *Just yours (₹X)* and *Whole queue (₹Y)*. It creates a `pending`
  row of kind `scope`, and only the asker's tap counts. Unlike variant questions, a new scope question
  replaces the asker's older one.
- Refused when an order is `unknown` ("resolve #N first"), when the queue (or the user's part of it) is empty,
  or when another order is between `claimed` and `checking_out`.
- **One open proposal at a time.** A new proposal marks the old one `superseded` and edits its card to say so.
- **Quote:** merge lines per `spin_id` (summing quantities), then call `update_cart` once. Its response already holds
  the full cart and bill, so we don't also call `get_cart`. From the cart:
  - Ignore ₹0 items whose `spinId` we didn't send (promo flyers).
  - **Unit or line price:** find out whether `discountedFinalPrice` is a unit price or a line price by checking
    which reading adds up to the "Item Total" bill line. Store unit prices in paise.
  - The cart's `selectedAddress` must match the pinned address (the part before `__`); if it doesn't, abort.
  - Parse rupee strings (`"₹12.60"`, `"FREE"`) to paise.
- **Unavailable items:** a spin that is missing, out of stock or capped below the requested quantity takes all
  its lines out of this proposal. Those lines stay queued and the card lists them as "not available now".
  If anything was dropped, quote once more without it so the bill is exact.
- **COD check:** `get_payment_options` → `cod.available`. If COD is unavailable, the card shows the bill
  without a Confirm button.
- **Card:**
  - Contents: lines with owners, Swiggy's bill lines, To Pay, and buttons *Confirm (COD) ₹X* / *Cancel*.
  - Callback data: `o:<order_id>:c` or `o:<order_id>:x`. Scope buttons use `s:<pending_id>:m|a`.
  - The card's message id is stored on the order (migration 002), so it can be edited later;
    `outbox.send` now returns the message id.

**Executor (`executor/order_executor.py`).**
1. **Confirm (any member):**
   - The atomic claim sets `orderer` and `claimed_at`; a lost claim answers "already handled".
   - Edit the card to "Placing order… (Name)".
2. **Stale check:** if a covered line is no longer queued or its quantity changed, supersede and re-propose,
   saying why.
3. **Re-quote:** if a spin is unavailable, or To Pay is above the proposal's To Pay × (1 + `PRICE_TOLERANCE`),
   supersede and post a new card that explains the change.
4. **`checkout(addressId, paymentMethod="Cash")`, called with no 429 retry:**
   - A 401 retry is allowed because the request was rejected before it ran.
   - Only `status: CONFIRMED` with an `orderId` counts as placed. Anything else (a tool error, a network error,
     a timeout, an odd response) goes to verify.
5. **Verify:** `get_orders(count=5)`.
   - `get_orders` lists the whole Swiggy account, including orders made in the Swiggy app, and Swiggy's orders
     carry no tag of ours. So a match has to agree on everything we know (tightened on 2026-11-12):
     - live (not failed, cancelled or unpaid), created after `claimed_at` minus 2 minutes, at our address;
     - total within ₹1 of the total sent to checkout (`bill_json.checkout_to_pay`, stored when the order moves to
       `checking_out`, so a restart can still use it; older orders fall back to the proposal's `to_pay`);
     - the same payment method (UPI or not);
     - at least half of our item names.
   - A UPI order whose Swiggy id we already hold matches on that id alone.
   - **Exactly one** match counts as placed with that id. Two or more are never guessed between: the order is
     `unknown` and a human runs `/resolve`. Item count isn't compared, because Swiggy adds ₹0 promo items.
   - Accepted risk: a wrong match now needs our checkout to have failed while one other order with the same
     total, method, address and items was placed in the same few minutes.
   - No match means `unknown`: tell the group to check the Swiggy app and run `/resolve <id> placed|failed`.
     New orders are blocked until then.
6. **Record (one transaction):**
   - The order becomes `placed`, with `swiggy_order_id`, `placed_at` and the final bill.
   - One `order_items` row per queue line, priced at unit price × that line's quantity, with its owners copied.
   - The lines become `ordered` and `queue_version` is bumped.
   - Shares: each line price is split equally between its owners. Leftover paise go to owners in ascending
     user id order. The orderer's own share is stored too.
7. **Result:** edit the card to "✅ Ordered by Name: Swiggy #id, ₹X cash on delivery", followed by
   "Owes Name: Priya ₹120, Rohan ₹80" (fees stay with the orderer).

**Cancel** (any member) works only while the order is `proposed`. Its lines are never touched.
**`/resolve <id> placed|failed`** moves into M4, because without it an `unknown` order would block ordering forever:
- `placed` records the order as in step 6, with no Swiggy id;
- `failed` marks the order failed and leaves its lines queued.

**`/history`** lists the last 10 orders. **`/history <id>`** shows items, owners and shares.

**Observability (small, ahead of M7):**
- Each lane event gets a short `turn_id`. It is stored on every `events` row (migration 002) and shown in log lines.
- `run_turn` logs one INFO line: rounds, tools called, input/output tokens, duration, outcome.
- The executor logs one INFO line per state change, with the order id.

No automated test or eval script may call `update_cart`, `checkout` or any other write tool on the real server.

### 7.2 `/address`: switching the household's delivery address

Added after M4 (2026-11-05), at the user's request.

- **Where the address lives:** the pinned address is now kept in `kv` under `address_id`. `SWIGGY_ADDRESS_ID`
  in `.env` is only the starting value, used when `kv` has none.
- **Read at use time:** every part of the bot that needs the address reads it when it uses it, through one
  helper. No component keeps its own copy from boot, so a switch applies at once with no restart.
- **`/address`:** shows the current label, then one button per address saved in the Swiggy account. It reads
  every page of `get_addresses`.
  - Buttons show the address tag (e.g. "HOME3"). If two tags are the same, the area is added to tell them apart.
    Full addresses, phone numbers and coordinates are never shown.
  - The current address is marked ✓.
  - Callback data: `a:<address id>` (under Telegram's 64-byte limit).
- **Who:** any member can switch, and the change is announced in the group: "Delivery address is now HOME3
  (changed by Angad)". It is also written to `events` and as an action line in chat memory.
- **On tap:**
  - The ID in the tap is untrusted: fetch `get_addresses` again and accept only an ID that is still saved.
  - **Open proposal:** marked `superseded`, and its card edited to "address changed, run /order again".
    Its prices were for the old address.
  - **Unknown order:** the switch is still allowed; ordering stays blocked until `/resolve` either way.
  - **Queue lines stay.** The re-quote at proposal time catches anything unavailable at the new address.
- **The order records its address:** each order stores the address it was proposed for (migration 003). Confirm
  supersedes the order if that address differs from the current one. This guards against an order going to the
  wrong flat even if a switch slipped past the step above.
- **`/status`** and the bill card show the address label.

### 7.3 UPI payment

Added on 2026-11-05. Response shapes are in `instamart_poc/results/calls/20261027_120214_checkout.json` and the
`check_payment_status` recordings next to it.

- **Card buttons:**
  - Row 1: *Pay by UPI ₹X* (`o:<id>:u`) and *Cash ₹X* (`o:<id>:c`). Row 2: *Cancel*.
  - The COD button is always shown, because Swiggy's `cod.available` flag proved unreliable; a refusal comes
    back as a clean checkout error.
  - The UPI app is set by `UPI_APP` in `.env` (default `gpay://upi/`).
- **The executor is shared:** claim, address check, stale check and re-quote run exactly as in 7.1. Only the
  checkout step differs. The order stores its `payment_method` (migration 004 also adds `payment_json`).
- **UPI checkout:** `checkout(addressId, paymentMethod="UPI", intentApp=UPI_APP)`, called once and never retried.
  - Expected answer: `status: PENDING_PAYMENT` with `orderId`, `paasId`, `bridgeUrl`, `pollingIntervalInMs` and
    `maxTimeToPollForInMs`. Store them in `payment_json` and Swiggy's `orderId` on the order.
  - Any other answer goes to the verify path, as in 7.1.
- **Pay message:** posted at once, replying to the card:
  - text: "<Name>: pay ₹X in the next 60 s. Don't pay after the button disappears."
  - one URL button, *Pay ₹X in <app>*, linking to `bridgeUrl`. The https link is used because Telegram buttons
    don't accept `gpay://` links.
- **Polling:**
  - Call `check_payment_status(paasId, orderId)` every `max(pollingIntervalInMs, 2000)` ms, until the status is
    terminal or `maxTimeToPollForInMs` + 15 s has passed. Status values are compared ignoring case.
  - The lane is busy meanwhile, so other group messages wait up to about 75 s. That is accepted for v1.
- **Success** (`isTerminalSuccess`, or status `success` / `paid`):
  - Call `confirm_order(orderId, paasId)`, then record the order as placed (7.1 step 6) under Swiggy's `orderId`.
  - Edit the pay message to "Paid ✅" with no button, and the card to the ✅ result.
  - If `confirm_order` errors, run verify: if the order is in `get_orders` it is placed, otherwise `unknown`.
- **Failure** (`isTerminalFailure`):
  - Mark the order `failed`, leave its lines queued, edit the pay message to "Payment failed; nothing was
    ordered", and remove the button.
  - Don't call `confirm_order` (Swiggy's docs say so).
- **Window closed (still pending):**
  1. Remove the Pay button **first**.
  2. Call `confirm_order` once, which Swiggy's docs say finalises the order as failed.
  3. Check `get_orders`. If the order is there, it is placed.
  4. Otherwise mark it `failed`, and say: "Payment window closed, nothing was ordered. If you paid anyway,
     Swiggy refunds it within ~4 days. If the order shows up in the Swiggy app, run /resolve <id> placed."
  - `/resolve` therefore also accepts a `failed` UPI order.
- **Clear refusals become `failed`, not `unknown`.** This applies to both COD and UPI. When checkout returns a tool
  error saying the order was not created ("Failed to create order…", "not eligible…") and verify finds no
  matching order, the order is `failed` and the card shows Swiggy's reason. Only network errors, timeouts and odd
  answers lead to `unknown`.
- **Restart:** an order in `checking_out` still becomes `unknown` on boot, as in 7.1.

**Splits:** each item's line price is divided equally among its owners. Each person's share is the sum
over their items. Fees are not split; the orderer pays them. The order record keeps each person's amount and
who it is owed to (the orderer).

### 7.4 M6 hardening details (2026-11-05)

- **Checkout time limit:**
  - `checkout` runs under `asyncio.timeout(CHECKOUT_TIMEOUT_S)`, default 45 s.
  - On timeout, close the MCP session, open a fresh one, and go to verify. Checkout is never retried.
  - UPI payment polling keeps its own deadline (7.3).
- **Restart recovery that checks with Swiggy:**
  - At boot, orders in `checking_out` become `unknown` (`claimed` and `quoting` are covered below). After Telegram
    is up and if Swiggy is logged in, each one is then checked against `get_orders`, using the same rules as verify.
    - A match counts as placed: record it (7.1 step 6) using the proposal's unit prices.
    - No match leaves it `unknown`, and the group gets "Order #N was interrupted by a restart during checkout.
      Check the Swiggy app, then /resolve N placed|failed."
  - **UPI pay message:** its id is stored in `payment_json`. On recovery, the pay message is edited so the Pay
    button disappears.
  - Orders that were only `claimed` or `quoting` never reached checkout, so they become `failed`
    ("interrupted before checkout, nothing was ordered") and need no `/resolve`.
- **No double add on login replay:** if Swiggy logs out partway through an agent turn that already changed the queue,
  the message is **not** parked for replay. The bot replies with what it already did and asks the person to repeat
  the rest after logging in, and the login link is posted as usual. Turns that changed nothing are parked as before.
- **Network drop mid-turn (B1):**
  - `TelegramOutbox` retries `send`, `edit` and `answer_tap` up to 3 attempts in total (sleeping 1 s, then 2 s;
    the sleep is injectable) when the failure is a connect error: a `NetworkError` caused by `httpx.ConnectError`,
    so the request never reached Telegram. Each retry is a `retry` event on the current span.
  - `TimedOut` is not retried on `send` (the message may have arrived and would post twice). It is retried on `edit`
    and `answer_tap`, where repeating is harmless.
  - If an exception escapes an agent turn after tools changed the queue, `run_turn` stores the user message and the
    action lines in `chat_memory` and raises `TurnFailedAfterChanges(actions, cause)` (`agent/errors.py`). The handler
    replies "Done so far: added #13 …; added #14 …. Then I hit a connection problem, so ask me again for the rest."
    (more than 5 actions are shortened to "and N more"). The message is never replayed. If that reply cannot be sent
    either, one warning is logged and the lane's generic apology is not reached.
  - A failure with no changes stores the user row and an assistant row "(failed: connection problem)", and a
    `NetworkError` is answered with "Something went wrong (connection problem?), nothing was changed. Try again."
    The Swiggy `LoggedOut` path with no changes is unchanged: nothing is remembered, so the parked replay starts clean.
  - A question or card that fails to send leaves no trace: `ask_variant` and the scope question delete their pending
    row, and a bill-card proposal whose card never posted is marked `cancelled`. The error then propagates as above.
  - The lane's "Something went wrong, try again." stays as a last resort for anything else.
- **Telegram network errors:**
  - `start_polling(error_callback=...)` logs a single WARNING line ("telegram polling: NetworkError: …")
    instead of a full traceback.
  - Repeated identical errors are logged at most once a minute.
- **Group upgraded to a supergroup:**
  - Telegram sends `migrate_to_chat_id` in the old group, and sending to the old id raises `ChatMigrated`.
  - Either way, the new id is stored in `kv.group_id` and used instead of `TELEGRAM_GROUP_ID`. A WARNING line asks
    the host to update `.env`.
  - The adapter, handlers and outbox all read the current group id. A send that hits `ChatMigrated` is retried once
    to the new id.
- **Expired pending rows:** variant and scope questions past `expires_at` are deleted at boot and hourly by a small
  background task that queues itself on the lane like any other event. An expired variant question leaves one
  action line in `chat_memory`.
- **Done when (live):**
  1. Tap *Pay by UPI*, don't pay, and run `docker kill` during the window.
  2. Then `docker compose up -d`.
  3. The Pay button disappears, the order resolves to `failed` or `unknown` with a group message, and new orders
     are blocked only if it is `unknown`.

## 8. Swiggy login (paste-back)

Why: Swiggy accepts only `http://localhost` or pre-approved HTTPS redirect URIs. A phone can't complete a
localhost redirect.

1. A Swiggy call fails with 401, or the refresh token is rejected. The auth state becomes `logged_out`.
2. The current turn is parked: its message and user are saved.
3. The bot posts in the group: "Swiggy is logged out. Open this link, log in with OTP. The page will fail to
   load at the end; that's expected. Copy the URL from the address bar and reply here with it within 2 minutes."
   The link is the OAuth authorize URL with PKCE and redirect `http://localhost:8765/callback`.
4. The user replies with `http://localhost:8765/callback?code=...&state=...`. The bot checks `state`, exchanges
   the code (valid 120 s) using the PKCE verifier it holds, stores the tokens, and replays the parked turn.

Posting this in the group is acceptable: the code is single-use, short-lived and useless without the
verifier, which never leaves the bot. Tokens are stored in the volume with file mode 0600. Access lasts
~5 days and refresh ~30 days (rotating), so a login is needed about once a month.

Built on the MCP SDK's `OAuthClientProvider`, as in the POC. The redirect handler posts to Telegram, and the
callback handler waits for the pasted URL instead of running a local HTTP server.

## 9. Data model (SQLite)

| Table | Key columns |
|---|---|
| `members` | `user_id`, `display_name`, `joined_at`, `active` |
| `queue_lines` | `id`, `spin_id`, `sku_id`, `name`, `variant`, `qty`, `est_unit_price`, `max_qty`, `original_text`, `added_by`, `added_at`, `status` (queued / ordered / removed), `slot` (short display number, set while queued; NULL otherwise), `order_id` (set when ordered) |
| `queue_line_owners` | `line_id`, `user_id` |
| `pending` | `id`, `user_id`, `kind` (variant / scope / login), `payload_json`, `created_at`, `expires_at`, `message_id` (006, the question's Telegram message) |
| `orders` | `id`, `status`, `scope`, `proposed_by`, `orderer` (who confirmed), `bill_json`, `swiggy_order_id`, `claimed_at`, `placed_at`, `error` |
| `order_proposal_lines` | `order_id`, `line_id`, `qty_at_proposal`, `price_at_proposal`. Which queue lines a proposal covers. |
| `order_items` | `order_id`, `name`, `variant`, `qty`, `line_price`, `spin_id`, `sku_id` |
| `order_item_owners` | `order_item_id`, `user_id` |
| `order_shares` | `order_id`, `user_id`, `amount`, `owed_to` |
| `events` | `id`, `ts`, `actor`, `kind`, `payload_json`, `turn_id` (002), `trace_id` (005, 32 hex chars, NULL when tracing is off). An audit log of every queue change, proposal, tap and Swiggy call. |
| `chat_memory` | `id`, `ts`, `role`, `user_id`, `text`, `to_user_id` (006, who an assistant row answered), `message_id` (006, its Telegram id) (rolling window) |
| `kv` | Settings and state: auth state, `last_nudge_queue_version`, and so on. |

Tokens live in `/data/tokens/` rather than in SQLite, so they can be wiped separately.

### 9.1 How the active queue is kept

- **Queue numbers are slots.** A new line takes the lowest positive number not used by a queued line and keeps it until it is ordered or removed (then `slot` is NULL). Users, the snapshot and the agent's tools see only slots; orders, history and events keep `id`.
- **The queue is the set of `queue_lines` with `status='queued'`.** There is no separate cart object;
  "the queue" is a query.
- **One line per request; the queue never merges lines.** If Priya asks for 1 Amul Taaza and Rohan asks for 2,
  that is two lines, so each person's quantity and share stay exact. Lines are merged only when the Swiggy cart
  is built: quantities are summed per `spin_id` and checked against `max_qty`.
- **A shared item is one line with several owners.** "2 maggi for me and rohan" is one line, qty 2, with 2 owners;
  its cost is split equally between them.
- **A proposal points at lines; it does not move them.** Lines stay `queued` and editable while a bill card is open.
  At Confirm the executor re-reads those lines. A removed line or a changed quantity means a fresh quote and a
  new bill card.
- **Placing an order** sets the covered lines to `status='ordered'` and `order_id`, and copies them, with the
  actual Swiggy prices, into `order_items` / `order_item_owners`. `order_shares` is computed from those.
  Cancelling changes nothing, because the lines never left the queue.
- **Removing** sets `status='removed'` instead of deleting, so the `events` log and history stay explainable.
- **Queue version:** a counter in `kv`, bumped on every queue change. The free-delivery nudge and stale bill
  cards compare against it.
- **No race:** everything, including the executor run, happens inside the household lane, so no edit can land
  between the Confirm tap and checkout.

## 10. Telegram surface

Chat is the main interface; commands are a few fixed shortcuts. The Telegram "/" menu and `/help` show exactly five:

| Command | What it does | Natural-language equivalent |
|---|---|---|
| `/help` | What the bot is and how to talk to it | |
| `/list` | The queue | "what's in the queue?" |
| `/order` | Order now. Bare: asks "everything or just mine?" when lines belong to more than one person, else proposes everything. `/order mine` and `/order all` are explicit. | "order now" / "I want X now" |
| `/address` | Switch the delivery address (command-only; anyone may tap the picker) | The agent says to use `/address` |
| `/status` | Swiggy login, address label, open proposal. In setup mode (no group id) it replies with the chat id. | "am I logged in?" |

Hidden (work when typed, not in the menu; `/help` ends with one line naming them): `/track [id]` (agent: `track_order`),
`/history [id]` (agent: `order_history`), `/login` (starts the paste-back login), `/resolve <id> placed|failed`
(clears an `unknown` order after checking the Swiggy app). `/login`, `/resolve` and `/address` change state, so the
agent never does them; it points to them.

Removed: `/add`, `/remove`, `/search`. Typing one gets a one-line nudge ("Just @mention me in plain words, e.g.
\"add 2 milk\"") and does nothing else. The agent does these from plain words.

The static prompt has a short "Commands people can use" list with when to suggest each, tells the agent to call
`track_order` for "where is my order", and never to suggest `/add`, `/remove` or `/search`.

Group setup:
- Add the bot to the group.
- **Group privacy mode must be OFF** (BotFather → Bot Settings → Group Privacy), then remove and re-add the bot.
  Verified in M1: with privacy on, Telegram delivers commands and replies to the bot but **not @mentions**.
  With it off the bot receives every group message; the gate drops everything not addressed to it, and
  dropped messages are never stored.
- Members are learned as they talk, or through `/join`.
- Inline buttons carry a pending or order ID. Variant buttons accept only the asking user's tap.
  Confirm and Cancel accept any member.
- The address is shown by its label only. The full address is never printed in the group.

## 11. Configuration (`.env`)

```
TELEGRAM_BOT_TOKEN=
TELEGRAM_GROUP_ID=            # the one household group; messages from other chats are ignored
OPENAI_API_KEY=
AGENT_MODEL=                  # OpenAI model for the agent loop
SWIGGY_MCP_URL=https://mcp.swiggy.com/im
SWIGGY_ADDRESS_ID=            # full id from get_addresses, e.g. d9lg...__AMVY...
FREE_DELIVERY_MIN=199         # approximate; the live check decides
CHECKOUT_TIMEOUT_S=45         # hard cap on one checkout call (section 7.4)
OTEL_EXPORTER_OTLP_ENDPOINT=  # e.g. http://phoenix:6006; empty = tracing off (section 16)
CARTMATES_TRACE_CONTENT=1       # 0 = no prompts, tool args/results or message text in spans
CARTMATES_TRACE_MAX_CHARS=8000  # cap per text attribute
PRICE_TOLERANCE=0.05
UPI_APP=gpay://upi/          # UPI app for the Pay button (section 7.3)
DATA_DIR=/data                # SQLite file + tokens (Docker volume)
LOG_LEVEL=INFO
```

## 12. Scaffold

```
cartmates/
  __main__.py            # boot: config, DB migrate, recover stuck orders, start the lane and Telegram
  config.py
  lane.py                # household FIFO worker
  telegram/
    adapter.py           # polling, update → lane events
    gate.py
    render.py            # bill card, queue list, variant buttons
  agent/
    llm.py               # LLM interface: OpenAILLM (Responses API) + ScriptedLLM for tests
    loop.py              # tool-calling turn, max 6 rounds
    tools.py             # tool schemas + dispatch to domain
    prompt.py
    memory.py            # transcript window + state snapshot
  domain/
    models.py
    queue.py
    pending.py
    orders.py            # propose, history
    splits.py
  executor/
    order_executor.py    # state machine, the only Swiggy writer
  swiggy/
    client.py            # MCP session, structuredContent parsing, backoff
    auth.py              # token store, paste-back OAuth
    catalog.py           # search / go-to parsing
    names.py             # short_name and button labels: display-time only, the DB keeps Swiggy's full name
    cart.py              # quote: update_cart + get_cart, ₹0 filtering
  store/
    db.py
    migrations/001_init.sql          # ... 002 orders, 003 order address, 004 payment, 005 trace id, 006 reply threading
tests/
  fakes/                 # fake Swiggy MCP server (recorded POC responses), Telegram stub, scripted LLM
  test_flows.py          # end to end: add → ambiguity → propose → confirm → history
  test_executor.py       # double tap, drift, out of stock, checkout error → verify → unknown
Dockerfile               # at the repo root, so `docker compose up` needs no flags
.dockerignore
compose.yml              # volume ./data:/data, restart: unless-stopped
docs/DESIGN.md
```

Decided: the old prototype (`app/`, `scripts/`, `tests/`, `setup.sh`) moves to `legacy/` when the build starts. Its
`tests/` folder is replaced.

## 13. Testing

- **End-to-end flows** drive the real lane, agent loop, domain and executor against:
  - a fake Swiggy MCP server that serves the 16 tools from recorded POC responses, with switches for
    out of stock, price rise, 401 and checkout timeout;
  - a stub Telegram;
  - a scripted LLM that returns fixed tool calls, so the tests are deterministic.
- **Executor tests** cover each failure path: double Confirm, drift, out of stock, checkout error, and restart mid-order.
- **Live smoke:** `instamart_poc/probe.py` stays the tool for checking the real server. It and the POC recordings
  cited in sections 7.1 and 7.3 live in the Queuify repo; CartMates doesn't need them to build or test. No automated test places
  a real order.
- **Agent quality:** a small eval set of real phrasings ("the usual milk", "2 maggi for me and rohan",
  "actually make it 3") run against the real OpenAI model before each prompt change.

## 14. Milestones

| # | Deliverable | Done when |
|---|---|---|
| M0 | Scaffold, config, SQLite migrations, Docker image | The container boots and the DB is created in the volume |
| M1 | Telegram adapter, gate, lane, `/help` `/list` `/status` | The bot answers in the group; non-mention chatter is ignored; one-at-a-time order is proven |
| M2 | Swiggy client + paste-back auth | `/login` works from a phone; tokens refresh; a 401 parks and replays a turn |
| M3 | Agent loop + catalog/queue tools + variant buttons | Natural-language add, edit and remove work; ambiguity asks; announcements post |
| M4 | Proposal + executor (COD) + splits + history | A real COD order is placed from the group, with correct per-person amounts |
| M5 | Free-delivery nudge with live quote | The nudge fires once per queue growth |
| M6 | Hardening: restart recovery, rate limits, logs, group-ID migration | Kill -9 mid-checkout ends as `unknown` and blocks new orders |
| M7 (built, not yet verified live) | Tracing: OpenTelemetry spans (lane → agent turn → LLM round → tool → Swiggy call; executor steps), exported over OTLP to Arize Phoenix as a second compose service | One trace shows a whole order from the message to the confirmed checkout; no tokens or auth codes in spans |
| v2 | Multi-household (section 17, milestones M8-M12) | Two households run side by side without crossing (17.8) |
| v3 | Deploy to a VPS | Same image and volume; only the hosting changes |
| later | Long-term notes memory (see 15.1) | "Shounak is vegetarian" said once still shapes picks a week later; notes are listed and removable |

### 15.1 Later: long-term notes memory

Today the agent forgets everything older than its window (the last 20 user messages within 12 h). Only the database
state (queue, orders, members) lasts. Preferences and decisions are lost: "Shounak is vegetarian", "we get the 12-egg
Licious pack", "Angad wants Pudina when it's in stock".

**Plan:** an agent-curated notes store, kept apart from the structured state.
- A `notes` table (id, about_user_id or household, text, created_by, created_at, last_used_at).
- Tools: `remember_note(text, about)`, `forget_note(id)`.
- The snapshot lists the notes (capped, most recently used first). `/notes` lists them for people; anyone can delete
  one.
- **Prompt:** save stable preferences and decisions, never one-off requests, and never anything sensitive beyond
  groceries.

**Why notes and not compaction:** the structured state is already rebuilt exactly each turn (the snapshot), so
summarising the chat would only add a lossy copy of it. Notes cover what the database doesn't model: soft, unplanned
facts.

**Why not move open questions or the queue into notes:** state that code acts on (button taps, orders, expiry) stays
in tables with transactions. Notes are only for facts that nothing executes on.

**Housekeeping that goes with it:** prune `chat_memory` older than 30 days (today it's never cleaned up).

## 15. Risks and open questions

- **Whose account?** Decided: anyone may run `/login` in v1, and the bot announces who logged in.
- **The host's own Instamart use** is affected: the live quote and checkout overwrite their Swiggy cart. Accepted.
- **Swiggy changes:** the docs list 20 tools while the server exposes 16, and the tools' descriptions change.
  The client should check the tool list at startup and fail loudly if a needed tool is missing.
- **Production access:** Decided: build and finish v1 and v2 on this machine first; apply for access, then do v3.
  Background: Swiggy whitelists production clients. Localhost prototyping works today; a household
  bot on a VPS may need an access application at `mcp.swiggy.com/builders/access`.
- **COD has a cart-value limit.** Seen on 2026-11-05: COD was available on a ₹242 cart but not on a ₹916 one, at the
  same address. The bill card already drops Confirm when COD is off. Large household orders may need splitting.
- **COD availability** can vary by address, cart or account. If `get_payment_options` has no COD, the
  proposal says so and doesn't offer Confirm.
- **Shared-item phrasing** ("for all of us") needs member names the LLM can match. The member list goes in the snapshot.
- **LLM cost:** gated messages only, plus a cap of 6 tool calls per turn.

## 16. Tracing (M7)

Agreed on 2026-11-05: finish the milestones first, then trace. Built in M7 (not yet verified against a live Phoenix).

### 16.1 What was built

- **Instrumentation:** OpenTelemetry, spans written by hand in `cartmates/tracing.py`. The HTTP libraries are **not**
  auto-instrumented (Telegram URLs contain the bot token), and no OpenInference auto-instrumentors are used. Only
  `openinference-semantic-conventions` is used, for attribute names, so Phoenix renders the spans properly.
- **Backend:** Arize Phoenix, a second compose service with its UI at `localhost:6007` (host port; Queuify uses 6006). Traces stay on this machine.
  Switching backends only means changing `OTEL_EXPORTER_OTLP_ENDPOINT`.
- **Off by default:** `tracing.setup(config)` installs a TracerProvider (resource `service.name=cartmates`, plus
  `openinference.project.name=cartmates`) with a BatchSpanProcessor and the OTLP/HTTP exporter only when the endpoint is
  set. Otherwise the no-op provider stays and tests need no collector. `tracing.shutdown()` flushes on clean exit.
- **Endpoint:** `OTEL_EXPORTER_OTLP_ENDPOINT` is the base URL (`http://phoenix:6006`); the exporter posts to
  `<endpoint>/v1/traces` (added by us, unless it is already there).
- **Config:**

| Variable | Default | Meaning |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP base URL; unset means tracing is off |
| `CARTMATES_TRACE_CONTENT` | `1` | `0` records no prompts, tool args or results, message text or reply text |
| `CARTMATES_TRACE_MAX_CHARS` | `8000` | Text attributes are cut to this, ending in `…[truncated N chars]` |

- **Never in spans:** Authorization headers, OAuth codes, the pasted login URL or text, token files, the bot token.
  The lane skips the text of any message the paste detection (`observe.looks_like_paste`, shared with the handlers)
  flags, and records only the exception type for a failure on it. `tracing.set_text` also refuses any text that looks
  like a paste. Telegram send/edit spans leave out the text when it holds a link (the Swiggy login link).

### 16.2 Span tree

```
event:<Message|CallbackTap|Recover|CleanupPending>     CHAIN, root, one per lane event
 |- agent.turn                                          AGENT
 |   |- llm.chat                                        LLM, one per round
 |   |- tool:<name>                                     TOOL, one per agent tool call
 |   |   '- swiggy:<tool>                               CHAIN
 |   '- telegram.send
 |- order.confirm                                       CHAIN, with span events (below)
 |   '- swiggy:<tool>, telegram.edit, telegram.send
 |- order.recover_unknown / order.recover_failed        CHAIN
 '- swiggy:<tool>, telegram.send / telegram.edit        (nudges, /status, ...)
boot.recover_stuck_orders                               CHAIN, root, runs before the lane exists
```

The hourly cleanup is a lane event, so it is a normal `event:CleanupPending` root.

### 16.3 Attributes

- **Root:** `session.id` (Telegram chat id, so Phoenix groups a chat's turns), `user.id`, `cartmates.chat_id`,
  `cartmates.command`, `cartmates.callback_data` (taps), `cartmates.turn_id`, `input.value` (message text, content capture on
  and not a paste), `cartmates.redacted` (a paste). Failures: exception event and ERROR status.
- **agent.turn:** `cartmates.rounds`, `cartmates.tools`, `cartmates.outcome`, `llm.token_count.prompt|completion|total`,
  `input.value`, `output.value`. Mirrors the INFO summary line.
- **llm.chat:** `llm.model_name`, `llm.input_messages.N.message.role|content`,
  `llm.output_messages.0.message.content` and `...tool_calls.N.tool_call.function.name|arguments`, token counts.
- **tool:\<name>:** `tool.name`, `tool.parameters` (the raw JSON), `output.value`, `cartmates.tool_error`.
- **swiggy:\<tool>:** `swiggy.tool`, `input.value` (args), `output.value` (result), `swiggy.status` (`ok` or the
  exception type), `swiggy.result_chars`; span events `token_refresh` and `rate_limited`. Checkout, confirm_order and
  payment calls are traced like the rest.
- **order.confirm:** `cartmates.order_id`, `cartmates.payment_method`, `user.id`, `session.id`. Span events: `claimed`,
  `quoting`, `checking_out`, `placed`, `unknown`, `failed`, `superseded`, and for UPI `pay_link_sent`,
  `payment_status`, `paid`, `window_closed`.
- **telegram.send / telegram.edit:** `session.id`, `telegram.text_length`, `output.value` (text, content capture on).

### 16.4 Trace ids in the database and logs

- `events.trace_id` (migration 005; migration 006 is the reply threading above) holds the 32 hex chars of the active trace, or NULL when there is no valid span.
  `turn_id` stays alongside it (and is also `cartmates.turn_id` on the root span).
- `/resolve` appends `Traces: <id>, ...` (up to three, newest first) for that order's events.
- Log records get `%(trace_id)s`, next to `%(turn_id)s`. The log format is unchanged.

### 16.5 Your Docker checklist (not done by Claude)

1. In `compose.yml`, add a `phoenix` service: image `arizephoenix/phoenix:<a specific recent version tag>`, ports
   `6007:6006`, environment `PHOENIX_WORKING_DIR=/mnt/data`, and a named volume mounted at `/mnt/data` (declare it under
   `volumes:`).
2. Add `OTEL_EXPORTER_OTLP_ENDPOINT=http://phoenix:6006` to `.env` (the bot service must be on the same compose network).
3. Rebuild: `docker compose up -d --build`, then open http://localhost:6007 and look for the `cartmates` project.

**Names.** Swiggy names carry marketing text. `short_name` drops everything after ` | ` and trailing parenthesised groups that read as claims (No, Not, Made with, Baked, Fried, Free). It is applied only when showing (agent view, buttons, `item_label`, cards, bills, snapshot, /list); stored names stay as Swiggy wrote them.

## 17. v2: multi-household

Drafted on 2026-11-09 from the decisions agreed on 2026-11-08 (`reference/MULTI_HOUSEHOLD_DECISIONS.md`; the
numbers in brackets, like (3d), point there). Sections 1-16 still describe one household. Where this section
disagrees with them, this section wins for v2.

**Version names.** v2 is multi-household on this Mac. Hosting on a VPS and friends' households come after Swiggy
grants production access (v3).

**Fresh start.** No data comes over from Queuify. The flat moves to a new Telegram group with the CartMates bot, and
the Queuify container is shut down. Until M8 that group runs the v1 code here, with its own new `data/cartmates.db`.

### 17.1 What changes, in one picture

```
 Telegram update
      │
      ▼
 adapter + gate ──► LaneRouter ── registry.db: (platform, chat_id) → household_id, status, budget
                        │           unknown chat → one setup reply, never the LLM
         ┌──────────────┼──────────────┐
         ▼              ▼              ▼
     lane hh-1      lane hh-2      lane hh-N        one FIFO worker per household, started on first use,
   households/1.db  households/2.db   ...           stopped after ~30 min idle; owns its DB connection
         │              │              │
         └──────────────┴──────────────┘
           shared, process-wide: LLM semaphore (4)
```

**One Swiggy account per household (assumed, not enforced).** Swiggy keeps one cart per account, and a household's
lane already owns its cart, exactly as in v1. So v2 needs no lock between households. Sharing an account between
households is deferred to v3 (17.10).

What stays the same: inside one household, everything in sections 3-16 runs unchanged. Each lane gets the same
`Handlers` object it has today, built for that household (its connection, Swiggy client, chat id). The domain code
already takes `conn` as an argument, so it doesn't know there is more than one household.

### 17.2 Storage

**Files** (2, 2a):

```
data/
  registry.db              which households exist and how to reach them
  households/1.db          one file per household: today's schema + secrets + settings
  households/2.db
  backups/                 copy of cartmates.db taken before 17.3 (never holds the master key)
```

**`registry.db`** (owned by the `LaneRouter`, the only writer):

| Table | Key columns |
|---|---|
| `households` | `id` (never reused), `status` (`active` / `inactive` / `broken` / `deleted`), `created_at`, `setup_by` (Telegram user id), `inactive_since`, `token_budget` (NULL = `.env` default) |
| `chats` | `platform` (`telegram`), `chat_id`, `household_id`. Primary key `(platform, chat_id)`. A supergroup upgrade adds the new id and drops the old one. |
| `invites` | `code`, `created_at`, `expires_at` (7 days), `used_at`, `household_id` |
| `login_links` | `code`, `household_id`, `user_id`, `expires_at` (10 min), `used_at`. The one-time code in the private-chat login button (17.5); it has to live here because a private chat doesn't belong to any household. |

**A household's DB:** migrations 001-007 as today, plus:
- `008_secrets.sql`: `secrets(name, ciphertext, updated_at)`. Holds the Swiggy tokens and client id, encrypted with
  `CARTMATES_MASTER_KEY` (Fernet, from the `cryptography` package, a new dependency) (3c).
- `009_usage.sql` (added in M10, with the budget that reads it): `token_usage(day, input_tokens, output_tokens)`,
  written in the same transaction as the turn's `chat_memory` rows (4c).
- `kv` keeps `address_id` and `address_label` (already there since 7.2). `group_id` leaves `kv` for `registry.chats`.

**Connections** (2b, the "DB connections" notes in the decisions):
- One owner per connection. A lane opens its file when it starts and closes it when it stops. No pool.
- Never `await` inside `db.transaction()`. The current code already follows this; a test keeps it that way (17.8).
- At boot, a **boot pass** opens each `active` household in turn: migrate, recover stuck orders (7.4), close. It
  writes down which households have orders to check with Swiggy, and those lanes start right away with a `Recover`
  event. A household whose migration fails is marked `broken` and logged, and the others still start (2c). A
  `broken` household's chat gets "This household is offline; the host has been told."

### 17.3 One-time switch from the v1 file (2a, 8c)

Nothing is imported from Queuify. The only v1 data is what the new CartMates group builds before M8, in
`data/cartmates.db`. The switch runs at startup when `registry.db` doesn't exist and `cartmates.db` does:
1. Copy `cartmates.db` (with SQLite's backup API, so the `-wal` contents are included) to
   `data/backups/v1-<timestamp>/`. The token file is not copied: a backup would be a plaintext token left on disk, and
   the refresh token rotates, so an old copy goes stale anyway.
2. Create `registry.db`, household 1 (`setup_by` = NULL), and `chats(telegram, <kv.group_id or TELEGRAM_GROUP_ID>, 1)`.
3. Move the file to `households/1.db` and run migrations 008+.
4. If `kv.address_id` is empty, copy `SWIGGY_ADDRESS_ID` into it.
5. Encrypt `tokens/swiggy.json` into `secrets`, read it back, and only then delete the file.
6. Log what to remove from `.env`: `TELEGRAM_GROUP_ID`, `SWIGGY_ADDRESS_ID`.

Each step checks whether it already happened, so a crash halfway and a second run both end in the same state.
With no v1 file (a clean install), boot creates an empty `registry.db` and households come only from `/setup`.

### 17.4 Routing and limits

**`LaneRouter`** (4a):
- `route(platform, chat_id)` → household id, or none. Unknown group: one reply ("This group isn't set up. Ask the
  host for an invite code, then send /setup <code>"), at most once a day per chat. No LLM call, no file opened.
- Private chats (1c): only `/start <login code>`, the login paste and `/setup` helpers. Anything else gets "I only
  work in your household group." A queue action is never taken from a private chat.
- A lane is created on first event, stopped after 30 min with nothing queued. The router's hourly ticker sends
  `CleanupPending` only to lanes that are running, so the ticker never keeps a household awake. A lane that starts
  runs the cleanup first. Expiry is already checked when a question is read, so the ticker only tidies up.
- Shutdown stops every lane as `Lane.stop` does today.

**Shared limits:**

| Limit | Where | Behaviour |
|---|---|---|
| LLM concurrency (4b) | `asyncio.Semaphore(LLM_CONCURRENCY)` around each model round in `agent/loop.py` | A round waits for a slot. Lanes keep each household to one turn at a time. |
| Daily token budget (4c) | before a turn, `SUM(token_usage)` for today in the household's DB vs `registry.households.token_budget` or `DAILY_TOKEN_BUDGET` | Over budget: free text gets "I've hit today's limit for this group; /order, /list and buttons still work." Commands and taps never check the budget. |
| Swiggy calls (4d) | none beyond the per-turn caps | |
| Swiggy cart (3d) | none in v2: one account per household, so the household's lane already owns the cart | See 17.10 for sharing an account |

### 17.5 Onboarding and login

**Setup** (5a-5d):
1. The host runs `python -m cartmates.admin invite` and sends the code to the new household.
2. Someone adds the bot to their group and sends `/setup <code>`. The router checks the code (unused, not expired),
   creates the household and its file, maps the chat, and records who ran it.
3. The bot asks for a Swiggy login (below), then shows the address picker from 7.2, then posts "Ready".
   Until there is an address the bot answers only setup commands.
4. Members are learned as they talk, as today (`handlers._record`).

**Login** (3a, 3e):
1. `/login` in the group, or a Swiggy call that finds the household logged out. The bot posts a button
   "Log in privately" that opens `t.me/<bot>?start=<code>` (a `login_links` row for that user and household).
2. In the private chat, `/start <code>` posts the Swiggy link and the paste-back instructions from section 8.
3. The paste in the private chat finishes the login for that household, and the bot says "Logged in by Angad" in
   the group. A parked turn is replayed in the group as today.
4. If the household already has a login, step 1 first asks "Replace the Swiggy login? (currently Angad's)" with
   Confirm / Cancel. Any member may confirm.

A login paste sent in the group is deleted if the bot can delete it, and answered with "Send that in our private
chat instead". It is never used.

**Leaving** (5e, 8d):
- The bot is removed from the group (a `my_chat_member` update; `ALLOWED_UPDATES` gains it): status `inactive`.
  Adding it back within 30 days makes it `active` again.
- After 30 days inactive, or right away after `/forget` and a confirmation tap: delete the file and its `-wal`, mark
  the row `deleted`, drop its `chats` rows. The id is never reused. Swiggy has no revoke; the refresh token expires
  within ~30 days.

### 17.6 Settings (6)

| Setting | v2 |
|---|---|
| `TELEGRAM_BOT_TOKEN`, `OPENAI_API_KEY`, `AGENT_MODEL`, `AGENT_REASONING_EFFORT`, `SWIGGY_MCP_URL`, tracing, `CHECKOUT_TIMEOUT_S`, `PRICE_TOLERANCE` | `.env`, unchanged |
| `TELEGRAM_GROUP_ID` | removed (read once by the switch in 17.3) |
| `SWIGGY_ADDRESS_ID` | removed (read once by the switch in 17.3) |
| `FREE_DELIVERY_MIN`, `UPI_APP` | `.env` default; a per-household override can come later |
| `CARTMATES_MASTER_KEY` | new, required once any household exists. Missing: the bot refuses to start with a clear error, like a bad Telegram token (decided 2026-11-14; with one household, starting logged out would only hide a config mistake). Present but unable to decrypt a household's secrets: that household is logged out and says so, and nothing else breaks. |
| `LLM_CONCURRENCY` (4), `DAILY_TOKEN_BUDGET` (300000) | new |

### 17.7 Observability

- **Boundary** (7a, 7b): deferred to v3. Telegram stays the only interface in v2, and the known `telegram` imports in
  `__main__.py`, `agent/loop.py` and `handlers.py` stay. The registry still keys chats by `(platform, chat_id)`, since a
  column with one value costs nothing and saves a migration later.
- **Tracing** (8a): every root span gets `cartmates.household_id`; `session.id` becomes `hh-<id>` instead of the chat
  id. Log lines get `[hh-<id> <turn_id>]`. Budgets come from the DB, never from Phoenix. Content capture stays on
  for now (8b).
- **Admin script:** `python -m cartmates.admin usage` reads each household file read-only (WAL allows it next to the
  running bot) and prints today's tokens per household and the total.

### 17.8 Milestones

Each milestone keeps every existing test passing, and the flat's CartMates group keeps working after each one.

| # | Builds | Proven by |
|---|---|---|
| **M8** Registry, silo files, switch | `registry.db` schema (all four tables; `invites` and `login_links` stay empty until M11); `households/<id>.db`; migration 008; secrets encryption; the boot pass; the switch in 17.3. The group id moves from `kv` to `registry.chats`, including the supergroup remap (pulled forward from M9, since `TELEGRAM_GROUP_ID` leaves `.env`). The `SWIGGY_ADDRESS_ID` fallback goes. Still one lane, now found through the registry. | `test_switch.py` and `test_registry.py`: a v1 fixture DB migrates to the expected registry rows and file; running it twice changes nothing; a crash after each step (injected) then a rerun gives the same result; no plaintext token file is left; the backup exists. A household with a corrupt file is `broken` and a second household still boots. A wrong master key leaves that household logged out. With no v1 file, boot creates an empty registry. Manual: the switch run on a copy of the group's `cartmates.db` first, then for real, and the group used normally afterwards. |
| **M9** LaneRouter | `LaneRouter`, one `Handlers` per household, idle stop, running-lanes-only ticker, unknown-chat reply, private-chat refusal, `hh-<id>` in spans and logs | `test_households.py`, two households in one process with the fakes: messages interleaved A, B, A, B add lines only to their own file; each turn's handler holds only its own connection (the test wraps `db.connect` and checks); a slow turn in A (blocked fake LLM) doesn't delay B; order inside A stays FIFO; an idle lane stops and restarts on the next message with its data intact; an unknown group gets one reply and the scripted LLM gets no call; a supergroup upgrade in A keeps A's household id. A test asserts no `await` happens inside `db.transaction` (the context manager fails if the running coroutine yields). |
| **M10** Shared limits | LLM semaphore; migration 009 and the token budget | With `LLM_CONCURRENCY=2` and four households, at most two fake model calls run at once. Over budget: free text gets the limit reply, `/order` and Confirm still go through; usage rows commit with the turn, and a turn that fails before commit adds none. |
| **M11** Onboarding and private login | `cartmates.admin invite` and `usage`; `/setup`; private-chat login with `login_links`; replace-login confirmation; setup address step; `my_chat_member` handling; `/forget`; 30-day deletion. A household file that `/setup` creates gets `kv.v1_switch_done`, so the switch (17.3) never treats it as the v1 file. | End to end with fakes: invite → `/setup` → private login → address → "Ready" → first add. A used, expired or made-up code is refused. No group message ever contains the Swiggy authorize URL. A login paste in the group is refused and not used. Replacing a login needs a confirm tap. Removing the bot makes the household `inactive`; at day 30 (injected clock) the file is gone, the row is `deleted`, and a new household gets a new id. |
| **M12** Live check with two households | The flat's CartMates group (household 1) and a test group (household 2, onboarded with an invite), each logged in to its own Swiggy account (the test group uses a second account of the host's) | Checklist: both groups add and list at the same time without crossing; a COD order placed in the test group and cancelled straight away in the Swiggy app; both groups `/order` at the same moment and each card shows only its own items; Phoenix shows `hh-1` and `hh-2` sessions; `cartmates.admin usage` matches the turns. |

No automated test calls a Swiggy write tool on the real server (section 7.1). M12 is the only real order, placed
by hand.

### 17.9 Open questions

- None open for v2 itself. The shared-account questions moved to 17.10.

### 17.10 Deferred to v3: sharing one Swiggy account between households

Not built in v2 (decided 2026-11-12). Recorded here so v3 starts from it.

**The problem.** A lane keeps one household's work in order, but Swiggy's cart belongs to the account. If two
households share an account, two lanes write one cart:
1. Household A taps Confirm, and the re-quote puts A's items in the cart.
2. Household B sends `/order`, and its quote replaces the cart with B's items.
3. A's checkout orders whatever is in the cart: B's items, paid by A.

Each lane did its own steps in order. The bug is between lanes. The proposal, the nudge and the executor's
re-quote all call `update_cart`.

**Possible fixes:**
- **Account lane (preferred).** One FIFO worker per Swiggy account owns the cart, in the same pattern as the
  household lanes. Household lanes stop calling `update_cart` and checkout and send it a job instead: "quote these
  lines" for a proposal or a nudge, or "quote, check out and collect payment" for Confirm. A job is the whole
  sequence, so nothing runs between a re-quote and its checkout. Only the account lane holds a Swiggy client that can
  write, so no code path can skip it. Cost: B's `/order` waits behind A's checkout, up to ~2 minutes with UPI. A nudge
  is skipped while the account lane is busy.
- **A process-wide lock** around the same three call sites does the same thing, but goes against the lane pattern.
  It has to sit at the call sites, because `asyncio.Lock` isn't reentrant and the executor's own re-quote would
  deadlock inside `cart.quote`.
- **Knowing the account.** Both need to know which households share an account. Swiggy gives no account id yet.
  Comparing saved address ids at login might work (unverified).

**Questions to answer first:**
- **Two logins on one account.** Each household's login is its own OAuth client and refresh-token chain. It's not
  known whether a second login logs the first out. Test with two scratch logins: a read call, a forced refresh and
  another read for each, checking the first after the second. If they conflict, store tokens per account in the
  registry, with households pointing at an account.
- **One OAuth client for all of CartMates**, instead of one per household. Production access will most likely
  approve one fixed client with an HTTPS redirect.
- **UPI wait.** Does `update_cart` during a pending UPI payment affect that order? If not, the account lane can take
  the next job during the payment wait.
- **Order matching** (7.1 step 5) is already strict enough for a shared account. The test group should still use
  a different saved address.
