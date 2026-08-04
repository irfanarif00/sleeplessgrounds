# Sleepless Grounds — Reservation System

## Context

`sleeplessgrounds.coffee` is a single-file static site (`index.html`, 2,619 lines) served from GitHub Pages, DNS on Cloudflare, mail on Google Workspace. There is no backend and no way for customers to book a table — the only interactive form on the site is a MailerLite newsletter embed.

The cafe needs online reservations: weekday nights only, 2PM–2AM, across three seating zones (indoor 30, outdoor 40, rooftop 20). Customers must get an emailed confirmation, the cafe must get a notification, and staff need an admin page to see bookings and control which nights/slots are open — including blacking out dates for private events.

Outcome: two new pages (`reserve.html`, `admin.html`) that look like they were always part of the site, backed by a Supabase project that costs $0/month.

A second goal follows from the first. The repo is currently **public**, because GitHub Pages only serves private repos on a paid plan. Once the site holds a booking system, the cafe wants the source private — so the final phase moves hosting to **Cloud Run behind Cloudflare** and switches the repo to private. That migration is sequenced *after* the reservation system ships: changing the host and building the booking flow at the same time makes any failure ambiguous.

## Decisions (confirmed with user)

| Area | Decision |
|---|---|
| Backend | Supabase — Postgres + Auth + Edge Functions |
| Service night | Keyed to the day the night **starts**. Fri 2PM → Sat 2AM is *Friday's* night and is bookable |
| Open days | Mon–Fri service nights (configurable) |
| Slots | Hourly, 14:00 → 00:00 inclusive (11 slots) |
| Turn time | 120 min, configurable. A booking holds seats for `[slot, slot+2h)` |
| Zones | Customer picks indoor / outdoor / rooftop; capacity tracked per zone |
| Confirmation | Auto-confirm if seats available; customer receipt + admin notification |
| Max online party | 10 pax. Larger → private-event inquiry (no seats held, email only) |
| Lead time | Min 2h, max 30 days ahead. Both configurable |
| Cancellation | Token link in the confirmation email. Modify = cancel + rebook |
| Admin auth | Supabase email + password, invite-only (public signup disabled) |
| Email | Resend, domain-verified |
| Bot protection | Cloudflare Turnstile, verified server-side |
| Timezone | `Asia/Manila` everywhere |
| Hosting (final) | Cloud Run (`asia-southeast1`), static nginx container, scale-to-zero |
| Custom domain | Cloudflare proxy + Origin Rule Host override — **not** a load balancer, **not** Cloud Run domain mapping |
| Repo visibility | Private, after the Cloud Run cutover |
| Direction videos | Moved to YouTube unlisted embeds; no longer served from origin |

## Architecture

```
GitHub Pages (public, unchanged host)
├── index.html          ← edit: nav entry + hero CTA only
├── reserve.html        ← new, customer booking flow
├── admin.html          ← new, staff dashboard
└── assets/ sg-base.css · sg-config.js · reserve.js · admin.js

Supabase (free tier, region ap-southeast-1 Singapore)
├── Postgres            ← zones, settings, blackouts, bookings, event_inquiries, admin_users
├── RPC get_availability()   SECURITY DEFINER, aggregate counts only → callable by anon
├── RPC book_reservation()   SECURITY DEFINER, advisory-locked, capacity-checked
├── Auth                ← admin accounts, signup disabled
└── Edge Functions      ← create-booking · cancel-booking (service role, hold the secrets)

Resend → customer receipt + admin notification
Cloudflare Turnstile → token verified inside create-booking
GitHub Actions (daily) → pings get_availability so the free project never pauses
```

**Trust boundary.** The `anon` key ships in public JS — that is fine and expected. Safety comes from grants, not secrecy:

- `anon` has **no SELECT/INSERT/UPDATE on `bookings`** at all. It can only `EXECUTE get_availability()`, which returns seat counts and never names, emails or phones.
- Writes go through the `create-booking` Edge Function, which holds the service-role key, the Turnstile secret and the Resend key as function secrets.
- `authenticated` admins read/write `bookings`, `settings` and `blackouts` under RLS gated on membership in `admin_users`.
- Because of this, `admin.html` living in the public repo is not a vulnerability. Do not over-engineer a private host for it.

## Data model — `supabase/migrations/0001_init.sql`

```sql
create type booking_status as enum ('confirmed','cancelled','completed','no_show');

create table zones (
  id text primary key,                   -- 'indoor' | 'outdoor' | 'rooftop'
  label text not null,
  capacity int not null check (capacity > 0),
  sort_order int not null,
  active boolean not null default true
);
-- seed: indoor/30, outdoor/40, rooftop/20

create table settings (               -- single row, id = true
  id boolean primary key default true check (id),
  open_days smallint[] not null default '{1,2,3,4,5}',  -- ISO dow of the service_date
  first_slot time not null default '14:00',
  last_slot  time not null default '00:00',
  slot_interval_minutes int not null default 60,
  turn_time_minutes int not null default 120,
  close_time time not null default '02:00',
  min_lead_hours int not null default 2,
  max_advance_days int not null default 30,
  max_party_online int not null default 10,
  notify_emails text[] not null default '{}',           -- admin recipients
  booking_enabled boolean not null default true
);

create table blackouts (              -- NULL = "all"
  id uuid primary key default gen_random_uuid(),
  service_date date not null,
  zone_id text references zones(id),   -- NULL → every zone
  slot_start time,                     -- NULL → whole night
  reason text,
  created_at timestamptz default now()
);

create table bookings (
  id uuid primary key default gen_random_uuid(),
  ref text not null unique,            -- 'SG-2608-A7F3', shown to the customer
  service_date date not null,          -- the night this belongs to
  starts_at timestamptz not null,      -- actual slot instant
  ends_at   timestamptz not null,      -- starts_at + turn_time at time of booking
  zone_id text not null references zones(id),
  party_size int not null check (party_size > 0),  -- no upper bound: zone capacity is
                                                  -- admin-editable, and staff may enter a
                                                  -- larger private event by hand
  name text not null,
  email text not null,
  phone text not null,
  notes text,
  status booking_status not null default 'confirmed',
  cancel_token uuid not null default gen_random_uuid(),
  created_at timestamptz not null default now(),
  cancelled_at timestamptz
);
create index on bookings (service_date, zone_id) where status = 'confirmed';
create index on bookings (cancel_token);

create table event_inquiries (         -- parties over max_party_online
  id uuid primary key default gen_random_uuid(),
  ref text not null unique, service_date date, preferred_time time,
  zone_id text references zones(id), party_size int not null,
  name text not null, email text not null, phone text not null,
  message text, handled boolean not null default false,
  created_at timestamptz not null default now()
);

create table admin_users (             -- id = auth.users.id
  id uuid primary key references auth.users(id) on delete cascade,
  email text not null, created_at timestamptz default now()
);
```

Store `ends_at` per row rather than deriving it — changing `turn_time_minutes` later must not silently rewrite what existing customers were promised.

### Capacity check — exact, not approximate

Summing every overlapping booking over-restricts: a 5PM–7PM and a 7PM–9PM booking do not coexist, yet both overlap a new 6PM–8PM one. Occupancy only changes at a booking's start instant, so the true maximum over `[start, end)` is the occupancy sampled at `{new start} ∪ {distinct starts_at of overlapping confirmed bookings}`.

Sample at those actual start instants, **not** at fixed hour marks. `slot_interval_minutes` lives in `settings` and is editable from the admin Schedule tab; if someone sets it to 30, hour-mark sampling skips every `:30` start and oversells silently with no error. Sampling at real starts is exact at any granularity and reuses the same overlap query.

### Two derivations that must exist exactly once

**`slot_list(settings)`** — the set of valid slot times. `last_slot = '00:00'` is *after* `first_slot = '14:00'`, so a naive `slot between first_slot and last_slot` is false for every slot including the 00:00 one. Generate by stepping from `first_slot` by `slot_interval_minutes` and wrapping past midnight, yielding `{14:00 … 23:00, 00:00}`. Both `book_reservation()` and `get_availability()` must call this one helper — two independent implementations of midnight wraparound is exactly how "the UI never offers a slot the write path would refuse" breaks.

**`starts_at` from `service_date + slot`** — the likeliest implementation error, and it makes every boundary test fail confusingly:

```sql
starts_at := ((p_service_date + (case when p_slot < s.first_slot then 1 else 0 end))
              + p_slot) at time zone 'Asia/Manila';
```

Without the day rollover the 00:00 slot lands 24 hours early. Without the explicit zone it resolves at the session default — UTC on Supabase — and everything shifts 8 hours. PH has no DST, so this is a one-time correctness issue rather than an ongoing hazard.

### `book_reservation(p_service_date, p_slot, p_zone, p_party, p_name, p_email, p_phone, p_notes)`

`SECURITY DEFINER`, in this order:

1. `pg_advisory_xact_lock(hashtext(p_zone || p_service_date))` — serializes concurrent bookings for the same zone-night. This is the oversell fix; a read-then-insert from the client would double-book the last seats.
2. Validate against `settings`: booking enabled, `extract(isodow from p_service_date)` in `open_days`, `p_slot in slot_list(s)`, party ≤ `max_party_online`, `starts_at ≥ now() + min_lead_hours`, `service_date ≤ current_date + max_advance_days`.
3. Reject if any `blackouts` row matches (date, and zone/slot either matching or NULL).
4. Capacity: for each sample instant defined above, sum `party_size` of `confirmed` bookings in that zone whose `[starts_at, ends_at)` contains the instant. Reject if `sum + p_party > zones.capacity`.
5. Insert and return `(ref, cancel_token, starts_at)`.

Raises distinct `SQLSTATE`s per failure so the Edge Function can map them to specific customer-facing messages — "that zone just filled up" is very different from "we need 2 hours' notice".

`get_availability(p_from date, p_to date)` — `SECURITY DEFINER`, returns `(service_date, slot_start, zone_id, capacity, seats_left, blocked, blocked_reason)`. Aggregates only, no PII. Uses `slot_list()` and the same open-day / lead-time / blackout rules as the write path. Granted to `anon`.

## Edge Functions

`supabase/functions/create-booking/index.ts`
1. CORS allowlist: `https://www.sleeplessgrounds.coffee` (+ `http://localhost:*` for dev).
2. Verify the Turnstile token against `siteverify`; reject on failure.
3. Per-IP rate limit (a small `rate_limits` table or Deno KV) as a second line of defense.
4. Validate and normalise input; if `party_size > max_party_online`, write `event_inquiries` and send the inquiry email instead.
5. Call `book_reservation()`. Map raised SQLSTATEs to HTTP 409/422 with a specific message.
6. Send both emails via Resend. **Email failure must not fail the booking** — the seat is already held; log and return success with a soft warning.

`supabase/functions/cancel-booking/index.ts` — **must not mutate on GET.** Gmail, Outlook/Defender and corporate link scanners prefetch URLs found in email bodies; a destructive GET means a scanner cancels the reservation before the customer has even opened the message, and "idempotent" is no defense because the *first* hit is the damaging one.

- `GET ?token=<uuid>` renders a small brutalist page (not JSON — this URL is clicked from an inbox) showing the booking details and a `CONFIRM CANCELLATION` button.
- `POST` with that same token performs the cancellation: `status='cancelled'`, `cancelled_at=now()`, idempotent, then emails both parties.
- An unknown or already-cancelled token renders a neutral page rather than leaking whether a booking exists.

Secrets: `SUPABASE_SERVICE_ROLE_KEY`, `RESEND_API_KEY`, `TURNSTILE_SECRET_KEY`, `NOTIFY_FROM`.

## Emails — `supabase/functions/_shared/email.ts`

Table-based HTML with fully inline CSS (no `<style>` block, no web fonts, no external images — Outlook and Gmail both strip them). Monospace receipt aesthetic on a dark charcoal card, with a plain-text alternative that carries the same information for clients that refuse HTML.

1. **Customer receipt** — `Reservation confirmed — SG-2608-A7F3`. Ref, date/time in Asia/Manila, zone, party size, the address, a "seats held until" line derived from turn time, and the cancel link.
2. **Admin notification** — to every address in `settings.notify_emails`. Same details plus phone and notes, subject prefixed for inbox filtering.
3. **Cancellation** — sent to both sides.
4. **Event inquiry** — to admins only; customer gets a "we'll be in touch" acknowledgement.

Send from `reservations@send.sleeplessgrounds.coffee`. The **subdomain matters**: the apex already carries Google Workspace SPF (`include:_spf.google.com`) and MX. Verifying a subdomain in Resend means adding new DKIM/SPF records under `send.` and never editing the apex records your staff email depends on.

## Frontend

### `assets/sg-base.css` (new)

Copy the `:root` token block (`index.html:47-59`), the reset, the hamburger/nav-overlay, fixed header and footer rules out of `index.html` into a shared stylesheet that `reserve.html` and `admin.html` link. Leave `index.html`'s inline `<style>` alone for now — pointing a 2,619-line page at an external sheet is a separate, riskier change. Migrating it later is a follow-up, noted so the duplication doesn't get forgotten.

### `reserve.html` (new)

Reuse the existing terminal-receipt component rather than inventing new visual language. The relevant classes already exist and should be lifted, not redrawn:

| Existing pattern | `index.html` | Reused for |
|---|---|---|
| `.menu-terminal` / `.menu-titlebar` / `.menu-dot` / `.menu-fname` | 562–621 | The booking window chrome, filename `sg_reserve.sh` |
| `.menu-opt` segmented control | 664–689 | Zone picker (indoor / outdoor / rooftop) |
| `.menu-row` / `.menu-fill` / `.menu-price` dotted leaders | 716–742 | Receipt lines on the confirmation step |
| `.menu-cut` "cut here" divider, `.menu-cursor` blink | 769–785 | Bottom of the printed receipt |
| `.section-header` + `// NN` label | 540–559 | Step headers |
| `.brutal-tab` | 898–920 | Date-strip / admin view tabs |
| `runFlicker()` + terminal boot timeline, `REDUCED_MOTION` | 2455–2500 | Step transitions, honouring reduced motion |

Flow, as five numbered steps inside one terminal window:

- `// 01 — DATE` Horizontal strip of the next 30 nights. Weekends and blacked-out dates render struck-through and disabled. Labels read `FRI 08` with `2PM–2AM` beneath, so the after-midnight hours are visibly part of that night.
- `// 02 — TIME` Monospace grid `14:00 … 00:00`, each cell showing remaining seats as a bar plus tabular numerals (`18:00 ▮▮▮▮▯ 22/30`). Full and blacked-out slots are disabled with a reason.
- `// 03 — AREA` `.menu-opt` segmented control with live seats-left per zone. Outdoor and rooftop carry a weather caveat.
- `// 04 — DETAILS` Name, email, phone, party size, notes. Inputs styled as terminal fields — 0 radius, 1px `--grid-line` border, amber focus ring, `>` prefix. Setting party size above 10 swaps the step into inquiry mode and says why. Turnstile widget sits above the submit button.
- `// 05 — RECEIPT` Printed receipt with dotted leaders, the `SG-####-XXXX` ref, and the `.menu-cut` divider. Adds a "SAVE THIS" note and the cancel link.

Availability is fetched once for the visible 30-day window and cached in memory; picking a date or zone re-filters client-side rather than re-querying. Re-fetch on submit failure so a 409 immediately shows accurate counts. Copy the GTM snippet (`index.html:6-11`) so the booking funnel shows up in existing analytics.

### `admin.html` (new)

Same tokens and chrome, presented as a control terminal — `sg_admin.sh`.

- **Login** — email + password against Supabase Auth, in a small terminal window. Session persisted by `supabase-js`.
- **Tonight** (default view) — every booking for the current service night, grouped by slot, showing zone, party, name, phone, notes. Actions: mark seated/completed, no-show, cancel, **reassign zone** (rain moves the rooftop indoors — this is a daily-use button, not an edge case; reassignment re-runs the capacity check on the target zone).
- **Upcoming** — 30-day list, filter by date/zone/status, search by ref or name, CSV export.
- **Schedule** — toggle which weekdays are open, edit slot range, turn time, lead time, max advance, max online party, and per-zone capacity/active. Writes `settings` and `zones`.
- **Blackouts** — add/remove blackouts at whole-night, single-slot, or single-zone granularity, with a reason. Shows a warning listing any already-confirmed bookings the blackout would strand, so staff can contact them; it never silently cancels.
- **Inquiries** — event inquiries with a handled toggle.

All admin reads and writes go straight through `supabase-js` under RLS — no extra Edge Function needed.

### `index.html` (edit — small and contained)

1. Add `<li><a href="/reserve.html" class="nav-link" data-num="// 07">Reserve</a></li>` after the Signal entry (line 1367). Append rather than renumber — the existing nav numbers are already offset from the section header labels, and re-sequencing would touch unrelated markup.
2. Add a `RESERVE A TABLE` CTA in the hero, styled off `.menu-opt.active` (amber fill, amber glow, 0 radius).
3. Add a booking line to the `// Status` info cell (line 1444) — the copy currently says "Open daily. No excuses.", which now needs to coexist with weekday-only *reservations*. Walk-ins stay welcome any day; make that explicit so the two facts don't read as a contradiction.

## Implementation order

Each phase ends in something verifiable. Follow TDD where there's logic to test — the SQL capacity function is the piece most worth testing first, and the cheapest to get wrong.

1. **Schema + seed.** `0001_init.sql`, `supabase start`, `supabase db reset`. Verify tables and seeds exist.
2. **`slot_list()` + `book_reservation()` + `get_availability()`, tests first.** A SQL test file (pgTAP or plain `do $$ ... assert`) covering: happy path; zone at capacity; the 5–7 / 7–9 / new-6–8 case that must *succeed*; the 00:00 slot accepted (the wraparound case); `slot_interval_minutes = 30` still refuses to oversell at `:30` starts; `starts_at` for a 00:00 slot resolves to the following calendar day at 16:00 UTC; weekend rejected; Fri 00:00 accepted as Friday's night; blackout at each granularity; under-lead-time; beyond max-advance; party > max_party_online; and a concurrency test issuing two parallel bookings for the last seats where exactly one must win.
3. **Edge Functions.** `create-booking` and `cancel-booking`, with Turnstile in test mode and Resend in a dev domain. Verify with `curl`, including the CORS preflight and a deliberately bad Turnstile token.
4. **`reserve.html` + `assets/sg-base.css` + `reserve.js`.** Point at the local Supabase first. Check mobile width, keyboard tab order, `prefers-reduced-motion`, and that a 409 mid-flow surfaces a readable message.
5. **`admin.html` + `admin.js`.** Create the first admin user, disable signups, confirm RLS by signing out and retrying every read.
6. **Integrate and harden.** `index.html` nav/CTA/copy edits, `.github/workflows/keepalive.yml` (daily `get_availability` ping), production secrets, real Turnstile keys, Resend domain live.
7. **Migrate hosting to Cloud Run, then make the repo private.** Detailed below. Deliberately last — the reservation system must be proven working on the current host first, so that if something breaks after the cutover it is unambiguously the cutover.

## Manual setup you'll need to do

1. Supabase project, region **ap-southeast-1 (Singapore)**. Set the DB timezone expectations to `Asia/Manila` in code (store `timestamptz`, convert at the edges).
2. Resend account → verify **`send.sleeplessgrounds.coffee`** → add its DKIM/SPF records in Cloudflare. Do not touch the apex SPF/MX that Workspace uses.
3. Cloudflare Turnstile → new site for `www.sleeplessgrounds.coffee` → site key (public, goes in `sg-config.js`) + secret key (function secret).
4. In Supabase Auth: create the staff account(s), insert matching rows into `admin_users`, and **turn off public signups**.
5. Set the four Edge Function secrets.
6. Fill `settings.notify_emails` with the addresses that should receive booking alerts.

## Verification

- **SQL:** `supabase db reset && psql -f supabase/tests/capacity_test.sql` — all assertions pass, including the parallel-booking race where exactly one of two competing requests wins.
- **PII isolation:** with only the anon key, `select * from bookings` must fail, and `get_availability()` must succeed and contain no name/email/phone. This is the single most important check — run it against production after deploy, not just locally.
- **End to end:** book from `reserve.html`, confirm the receipt arrives at a real inbox, the admin notification arrives, the booking shows in **Tonight**, the cancel link frees the seats, and the freed seats reappear in availability.
- **Cancel link is prefetch-safe:** `curl` the cancel URL (a plain GET, exactly what a mail scanner does) and confirm the booking is *still* `confirmed` afterwards. Only the `CONFIRM CANCELLATION` POST may cancel it. Clicking the link by hand passes either way, so this check has to be done with curl.
- **Boundary cases by hand:** a Friday 00:00 slot must be bookable and must appear under Friday in the admin view; a Saturday must be unbookable; a slot inside a blackout must be refused with its reason.
- **Deliverability:** send to Gmail and Outlook, check both land in the inbox and that DKIM/SPF pass in the raw headers.
- **Free-tier survival:** run the keepalive workflow manually once and confirm it returns 200.

## Cost

$0/month, before and after the migration.

- **Supabase** free tier — pauses only after ~7 days of zero DB traffic, which the daily ping prevents.
- **Resend** free tier — 3,000/mo but **capped at 100/day**. This is the ceiling to watch: each booking sends 2 emails, so ~50 bookings/day.
- **Turnstile**, **Cloudflare** proxy/cache, **GitHub Actions** — free.
- **Cloud Run** — free tier covers 2M requests/month. Egress is free only for the first 1 GB/month, then $0.12/GB, which is precisely why the videos move to YouTube; without that, the MP4s were the one line item that could produce a surprise bill. Artifact Registry's 0.5 GB free tier comfortably holds a ~15 MB image.

First real cost would be Supabase Pro ($25/mo), and only if the cafe outgrows the free database or wants the no-pause guarantee without the cron ping.

## Phase 7 — Move hosting to Cloud Run, make the repo private

Runs only after the reservation system is live and verified on GitHub Pages.

### Why not just keep Pages

GitHub Pages serves a private repo only on a paid plan, and **even then the published site stays public** — repo visibility and site visibility are separate things. So paying doesn't buy privacy of the site, only of the source.

| Path | Monthly | Verdict |
|---|---|---|
| **Cloud Run + Cloudflare proxy** | ~$0 | **Chosen.** Free tier covers the traffic; Cloudflare absorbs repeat requests |
| GitHub Pro + Pages | $4 | Works and needs zero migration — noted for the record, but keeps hosting off GCP |
| Cloud Run + global external ALB | ~$18+ | Rejected. The forwarding rule alone costs more than every alternative |
| Firebase Hosting | $0 | Rejected. 360 MB/day transfer cap; ~5 visitors/day would exhaust it at current asset weight |

### The container is an allowlist, not a copy of the repo

This is the load-bearing detail. A container that serves the repo root would serve **`.plans/reservation-system-plan.md`** — the dot-directory exclusion protecting it today is *Jekyll behavior*, and Jekyll disappears with Pages. It would also keep serving the ~145 MB of files the site never references, which Pages is already exposing today: `verification_*.png` (dev screenshots), `benchmark.html`, `Gemini_Generated_Image_*.png`, `verify_glassmorphism.py`, `.DS_Store`, `menu-images/`, `background-image.png`, `sleepless-grounds-logo.png`, and two unreferenced menu PDFs (24 MB + 9.1 MB).

A grep of every `src`, `href` and CSS `url()` in `index.html` shows the site actually serves only these:

```dockerfile
FROM nginx:1.29-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
WORKDIR /usr/share/nginx/html
COPY index.html reserve.html admin.html logo-cropped.png ./
COPY assets/ ./assets/
COPY images/ ./images/
```

That is the whole site — ~4.5 MB, image ~15 MB. Nothing is deleted from git; the unreferenced files stay archived in the repo and simply stop being published. Re-run the reference grep before finalising, and add `location ~ /\. { deny all; return 404; }` to `nginx.conf` as a second layer so no dotfile is ever reachable even if the allowlist grows sloppy.

`nginx.conf` also needs: `listen 8080` (Cloud Run's `$PORT`), long `Cache-Control` on `images/` and `assets/`, short on the HTML, and `gzip on`.

### Cloud Run service

```
gcloud run deploy sg-site --region asia-southeast1 \
  --min-instances=0 --max-instances=3 --memory=128Mi --cpu=1 \
  --port=8080 --allow-unauthenticated
```

`min-instances=0` keeps idle cost at zero at the price of a cold start (~1s for nginx) for whoever arrives after a quiet spell. With Cloudflare caching in front, most requests never reach the origin, so cold starts are rare — accept them rather than pay for a warm instance.

### Custom domain — Cloudflare proxy, not domain mapping

Cloud Run's native domain mapping is supported in `asia-southeast1`, but Google still labels it preview and explicitly "not production-ready" on latency grounds, and Cloudflare blocks its ACME validation — which would force the orange cloud **off**, losing the edge caching that keeps egress near zero. Every renewal would re-open that footgun. Rejected.

Instead:

1. `www` → CNAME to `sg-site-xxxx.asia-southeast1.run.app`, **proxied** (orange cloud).
2. An **Origin Rule** with *Host Header Override* set to that same `run.app` hostname. Without this, Cloud Run receives `Host: www.sleeplessgrounds.coffee`, doesn't recognise it, and returns 404 — this single rule is the difference between a working site and a blank 404. Note it is an **Origin** Rule; Transform Rules cannot set the Host header.
3. Cache Rules covering `images/*` and `assets/*` with a long edge TTL.
4. Keep the existing apex → `www` redirect.
5. Delete the repo's `CNAME` file (a Pages-only artifact), then **turn Pages off** in repo settings so two origins can't serve stale copies.

Cut over by leaving Pages live until the Cloud Run URL is verified directly, then flipping DNS. Rollback is one DNS change.

### Videos move to YouTube

The three MP4s (43 + 26 + 31 MB) leave the origin and become unlisted YouTube embeds in the directions tabs. Three reasons, in order of weight: Cloudflare Free's ToS §2.8 restricts caching large volumes of video, so edge caching cannot honestly be relied on to absorb them; Cloud Run egress is free only for the first 1 GB/month and then bills at $0.12/GB; and a 43 MB non-adaptive MP4 is punishing on Philippine mobile data, which YouTube's adaptive bitrate fixes outright. Keep the `.brutal-tab` switcher exactly as it is — only the panel contents change, and the iframes should be `loading="lazy"` so they don't cost anything until a tab is opened.

### Deploy pipeline

`.github/workflows/deploy.yml` — on push to `main`: authenticate via **Workload Identity Federation** (`google-github-actions/auth@v2`), build, push to Artifact Registry, `gcloud run deploy`. No service-account JSON key in the repo — it is about to hold real secrets, and a long-lived key there is the worst thing in this plan if it leaks.

Private repos get 2,000 free Actions minutes/month instead of unlimited. A ~2-minute deploy plus the ~10-second daily keepalive lands far under that.

### What does not change

Nothing about Supabase, the Edge Functions, CORS, Turnstile or Resend — the origin hostname stays `www.sleeplessgrounds.coffee` throughout, so every allowlist and site key remains valid. The keepalive workflow is unaffected.

### The warning that matters

**A private repo is not a security control for `admin.html`.** The deployed site stays fully public on Cloud Run exactly as it is on Pages, so anyone can still fetch `admin.html` and read `sg-config.js`. The only things protecting customer data are the `anon` role having no grants on `bookings` and the RLS policies gated on `admin_users` — unchanged, and still the entire boundary. Do not let "the repo is private now" become a reason to loosen either.

### Verification

- `curl` the `run.app` URL directly: `index.html` 200, `/.plans/reservation-system-plan.md` **404**, `/benchmark.html` **404**, `/verification_gallery.png` **404**.
- After DNS cutover, the same checks against `https://www.sleeplessgrounds.coffee` — plus confirm it is *not* a 404 on the homepage, which is what a missing Origin Rule looks like.
- Book a reservation end to end on the new origin to prove CORS and Turnstile still pass.
- Confirm Pages is disabled and `irfanarif00.github.io/sleeplessgrounds/` no longer serves the site.
- Flip the repo to private, then re-run the booking flow — it must be unaffected, proving nothing depended on repo visibility.

## Deliberately out of scope

Deposits and online payment; SMS reminders; waitlists; recurring/series bookings; a customer account system; walk-in/POS integration; multi-language. Self-service *editing* is also out — cancel-and-rebook covers it with far less surface area. The `cancel_token` column is added now so a fuller self-service page can be built later without a migration.

Also out: moving the booking API off Supabase Edge Functions onto Cloud Run. Once Cloud Run exists it becomes technically possible to consolidate, but Postgres still has to live on Supabase, so it would trade one platform for two moving parts and re-open a decision already made. Deleting the ~145 MB of unreferenced files from git is likewise out of scope — the container allowlist stops publishing them, which is the part that matters.

## Known follow-up

`index.html` keeps its own copy of the `:root` tokens and nav CSS, duplicating `assets/sg-base.css`. Worth reconciling in a dedicated change once the reservation pages are stable, so a token edit doesn't have to be made twice.
