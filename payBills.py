"""
payBills.py — QuickBooks Online bill-payment helper.

Logs in to QBO with the same persistent-browser-session flow as
ETAutomations, opens the Pay Bills page, filters to the MAERSK payee and
the Wells Fargo payment account, clears any pre-selected rows, then keys
in a payment amount for every booking listed in inv_booking_nums.csv.

CSV format — one bill per line, no header needed:

    {inv_num},{bookingNumber},{open_balance}
    e.g.  INV-1042,254545455,1250.00

The browser stays open after the last row so you can review and submit the
bill payment yourself; press ENTER in this console when you are done.

Usage:
    python payBills.py [path/to/inv_booking_nums.csv]

The first run opens a visible Chromium window — log in to QuickBooks there.
The session is saved to a persistent profile, so later runs skip the login.
"""

import csv
import os
import re
import sys
import traceback
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import Page, expect, sync_playwright, TimeoutError as PWTimeout

load_dotenv(override=True)

# ── Constants ──────────────────────────────────────────────────────────────────
QBO_WEB          = "https://qbo.intuit.com"
BILL_PAYMENT_URL = f"{QBO_WEB}/app/billpayment"
# Same persistent profile as ETAutomations, so an existing QBO login is reused.
SESSION_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "QB-APITesting" / "qbo_browser_session"
QB = os.getenv("QB") if os.getenv("QB") is not None else ""

PAYEE_NAME      = "MAERSK"
PAYMENT_ACCOUNT = "Wells Fargo"
CSV_NAME        = "inv_booking_nums.csv"


# ── QuickBooks login logic (from ETAutomations) ────────────────────────────────
def _is_on_auth_page(page) -> bool:
    return any(h in page.url for h in ("accounts.intuit.com", "login.intuit.com", "/login"))


def _wait_for_qbo_app(page, timeout: int = 180_000):
    """Block until QBO app shell loads (not login/auth pages)."""
    if _is_on_auth_page(page):
        print("\n[browser] Please log in to QuickBooks in the browser window.")
        print("[browser] Waiting up to 3 minutes…")
        page.wait_for_url("**/app/homepage?**", timeout=timeout)
        page.wait_for_load_state("load", timeout=15_000)
        print("[browser] Logged in - continuing.\n")


def launch_and_login(pw):
    """Open the persistent QBO browser session and make sure we're logged in."""
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    context = pw.chromium.launch_persistent_context(
        str(SESSION_DIR),
        headless=False,  # the run ends with a manual review/submit step
        slow_mo=200,
        viewport={"width": 1440, "height": 900},
    )
    page = context.new_page()
    page.goto(QBO_WEB, wait_until="load")
    if _is_on_auth_page(page):
        try:
            page.locator("button[data-testid='AccountChoiceButton_0']").click(timeout=10_000)
            page.fill('input[name="Password"]', QB)
        except Exception as e:
            print(f"Error clicking AccountChoiceButton: {e}")
    _wait_for_qbo_app(page)
    return context, page


# ── CSV loading ────────────────────────────────────────────────────────────────
def load_booking_rows(csv_path: Path) -> list:
    """Parse {inv_num},{bookingNumber},{open_balance} lines into
    [{"inv_num": ..., "booking_num": ..., "balance": ...}, ...]."""
    if not csv_path.exists():
        sys.exit(f"CSV file not found: {csv_path}")

    entries = []
    with csv_path.open(newline="", encoding="utf-8-sig") as fh:
        for line_num, row in enumerate(csv.reader(fh), start=1):
            if not row or not any(cell.strip() for cell in row):
                continue  # blank line
            if len(row) < 3:
                print(f"  [CSV] Line {line_num}: expected 'inv_num,booking,balance' — skipping {row!r}")
                continue
            inv_num = row[0].strip()
            booking = row[1].strip()
            # Join the remaining cells so an unquoted thousands separator
            # ("1,250.00" split across two cells) still comes through intact.
            balance = "".join(cell.strip() for cell in row[2:]).replace("$", "").replace(",", "")
            if line_num == 1 and not re.search(r"\d", balance):
                continue  # header row
            if not inv_num or not booking or not balance:
                print(f"  [CSV] Line {line_num}: empty field — skipping {row!r}")
                continue
            entries.append({"inv_num": inv_num, "booking_num": booking, "balance": balance})
    return entries


# ── Pay Bills page helpers ─────────────────────────────────────────────────────
def _fill_typeahead(page: Page, locator, value: str, label: str):
    """Type into an Intuit typeahead combobox and pick the match with ENTER."""
    locator.wait_for(state="visible", timeout=60_000)
    locator.click()
    locator.fill("")
    locator.press_sequentially(value, delay=80)
    page.wait_for_timeout(1_500)  # give the suggestion list time to populate
    locator.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2_000)  # let the bills table refresh
    print(f"  [QBO] {label} set to {value!r}")


def _set_select_all(page: Page, checked: bool):
    """Check/uncheck the 'Select all rows' checkbox at the top of the table."""
    box = page.locator("input[aria-label='Select all rows']").first
    box.wait_for(state="attached", timeout=30_000)
    if box.is_checked() != checked:
        try:
            box.set_checked(checked, force=True)
        except Exception:
            box.evaluate("el => el.click()")  # styled checkbox — click the input directly
    page.wait_for_timeout(1_000)
    if box.is_checked() != checked:
        raise RuntimeError(
            f"Could not {'check' if checked else 'uncheck'} the 'Select all rows' checkbox."
        )
    print(f"  [QBO] Select all rows {'checked' if checked else 'unchecked'}.")


def _confirm_amount_paid_zero(page: Page):
    """Verify the amount-paid total reads $0.00 before entering payments."""
    amount = page.locator("p[data-testid='amount-value']").first
    amount.wait_for(state="visible", timeout=30_000)
    scoped = page.locator("p[data-testid='amount-value'][class*='amountPaidValue']")
    if scoped.count() > 0:
        amount = scoped.first
    try:
        expect(amount).to_have_text("$0.00", timeout=10_000)
    except AssertionError:
        raise RuntimeError(
            f"Amount-paid check failed: expected $0.00, page shows {amount.inner_text().strip()!r}."
        )
    print("  [QBO] Amount paid confirmed at $0.00.")


def enter_payments(page: Page, entries: list) -> list:
    """For each booking, filter the bill list and key in its payment amount.
    Returns the inv_nums of the rows that were successfully filled."""
    find_bill = page.get_by_test_id("find_bill_no")
    filled_inv_nums = []
    skipped = 0
    for i, entry in enumerate(entries, start=1):
        inv_num, booking, balance = entry["inv_num"], entry["booking_num"], entry["balance"]
        print(f"{'─'*50}")
        print(f"[{i}/{len(entries)}] Invoice {inv_num} · Booking {booking} — payment {balance}")

        find_bill.click()
        find_bill.fill("")  # clear whatever was searched before
        page.wait_for_timeout(400)
        find_bill.fill(booking)
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(1_500)  # let the table re-filter

        payment = page.get_by_test_id("outstanding_table_payment_1")
        try:
            payment.wait_for(state="visible", timeout=15_000)
        except PWTimeout:
            print(f"  [QBO] No bill row found for {booking!r} — skipping.")
            skipped += 1
            continue
        payment.click()
        payment.fill(balance)
        payment.press("Tab")  # commit the amount so QBO registers the payment
        page.wait_for_timeout(800)
        filled_inv_nums.append(inv_num)
        print(f"  [QBO] Entered {balance} for booking {booking} (invoice {inv_num}).")
    print(f"{'─'*50}")
    print(f"Done entering payments: {len(filled_inv_nums)} entered, {skipped} skipped.")
    return filled_inv_nums


# ── Main ───────────────────────────────────────────────────────────────────────
def _default_csv_path() -> Path:
    script_dir = Path(__file__).resolve().parent
    for cand in (Path.cwd() / CSV_NAME, script_dir / CSV_NAME):
        if cand.exists():
            return cand
    return Path.cwd() / CSV_NAME  # load_booking_rows reports the missing file


def pause_before_exit():
    try:
        input("\nPress ENTER to close this window...")
    except EOFError:
        pass


def main():
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_csv_path()
    entries = load_booking_rows(csv_path)
    if not entries:
        sys.exit(f"No usable rows in {csv_path} - nothing to do.")
    print(f"Loaded {len(entries)} booking(s) from {csv_path}.")

    with sync_playwright() as pw:
        context, page = launch_and_login(pw)

        print("\n[QBO] Opening bill payment page...")
        page.goto(BILL_PAYMENT_URL, wait_until="load")
        if _is_on_auth_page(page):
            raise RuntimeError(
                "Session expired. Delete the qbo_browser_session folder and re-run to log in again."
            )

        _fill_typeahead(page, page.get_by_test_id("vendor_quickfill__textField"), PAYEE_NAME, "Payee")
        _fill_typeahead(page, page.get_by_test_id("payment_account__textField"), PAYMENT_ACCOUNT, "Payment account")

        print("  [QBO] Selecting all rows, then clearing the selection...")
        _set_select_all(page, True)
        _set_select_all(page, False)
        _confirm_amount_paid_zero(page)

        filled_inv_nums = enter_payments(page, entries)

        inv_num_string = ",".join(filled_inv_nums)
        print(f"{'─'*50}")
        print("Successfully filled invoice numbers:")
        print(inv_num_string)
        print(f"{'─'*50}")

        print("\nAll payments are keyed in. Review and submit the bill payment in the browser.")
        try:
            input("Press ENTER here AFTER you have finished the bill payment to close the browser...")
        except EOFError:
            pass
        try:
            context.close()
        except Exception:
            pass  # browser may already have been closed by hand
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("\n❌ An error occurred:\n")
        traceback.print_exc()
        pause_before_exit()
        sys.exit(1)
