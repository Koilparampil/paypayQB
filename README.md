# paypayQB

Playwright automation for QuickBooks Online bill payments. Logs in to QBO
(reusing the same persistent browser session as ETAutomations), opens the
[Pay Bills page](https://qbo.intuit.com/app/billpayment), filters to the
**MAERSK** payee and the **Wells Fargo** payment account, clears any
pre-selected rows, then keys in a payment amount for every booking listed in
`inv_booking_nums.csv`. The browser stays open at the end so you can review
and submit the bill payment yourself.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env   # then fill in your QuickBooks password
```

## Input file

Create `inv_booking_nums.csv` next to `payBills.py` (or pass a path on the
command line). One bill per line, no header needed:

```csv
{inv_num},{bookingNumber},{open_balance}
INV-1042,254545455,1250.00
INV-1043,254545456,987.65
```

See `inv_booking_nums.example.csv` for a template. The real CSV is
git-ignored so booking data never gets committed.

## Run

```bash
python payBills.py                       # uses ./inv_booking_nums.csv
python payBills.py path/to/other.csv     # or an explicit path
```

What happens:

1. A Chromium window opens on qbo.intuit.com. If you aren't logged in, the
   script picks the first saved account, pre-fills the password from `.env`,
   and waits up to 3 minutes for you to finish signing in (MFA etc.). The
   session persists, so later runs skip this.
2. It navigates to the Pay Bills page and sets Payee = MAERSK and
   account = Wells Fargo.
3. It checks then unchecks **Select all rows** and confirms the amount-paid
   total reads **$0.00** before touching anything.
4. For each CSV row it types the booking number into **Find Bill No.**,
   waits for the table to filter, and enters the open balance into the
   **Payments** field of the matching row. Bookings with no matching bill
   are reported and skipped.
5. It prints a comma-separated string of the `inv_num`s that were
   successfully filled (skipped bookings are left out), so you have a record
   of exactly which invoices were paid.
6. It stops and waits — review the payment in the browser, submit it
   yourself, then press ENTER in the console to close the browser.
