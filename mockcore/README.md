# CoreLink mock core-banking client

A deliberately legacy-style Java Swing desktop app. It is the proxy target for the
computer-use system. Everything in it is fake: members, SSNs from the never-issued 900 series,
operators, and balances.

## Run

Needs JDK 17 or newer. The run script uses a portable JDK in `.tools/` if one is present.

```powershell
.\mockcore\run.ps1                    # tenant "heritage", CoreLink 7.4.1
.\mockcore\run.ps1 -Tenant lakeshore  # same product, v7.6.2, different labels plus a disclosure step
```

## Fake credentials

| Who | ID | Password | Can do |
|---|---|---|---|
| Teller | teller01 | demo123 | Inquiry, open sub-account up to $10,000 |
| Inquiry clerk | viewer01 | demo123 | Inquiry only; open sub-account is denied |
| Supervisor | sup01 | demo123 | Keys the override above the teller limit |

## Seeded members

| Member | Status | Useful for |
|---|---|---|
| 12345 | Active | Happy path. Savings $4,210.55 |
| 23456 | Active | Large balances, operator-limit override |
| 34567 | Restricted | Business outcome: restricted member |
| 45678 | Closed | Business outcome: closed membership |
| 99999 | Missing | Business outcome: MBR NOT ON FILE |

## Flows

1. **Member Inquiry** (F2). Enter a member number and read name, status, and share balances.
2. **Open Sub-Account** (F5, tran 0417). Load member, choose a type, enter a deposit and funding
   account, review, then post. Posting is irreversible and asks for confirmation.

## Runtime conditions it can produce

| Kind | How |
|---|---|
| Validation error | Bad member number format, bad amount, below minimum, insufficient funds |
| Record not found | Member 99999 |
| Permission denial | Sign on as viewer01, or set `permission_denied=true` |
| Unexpected dialog | `popup=true` |
| Session expiry | `session_expired=true` fires once per file edit; also a real idle timeout |
| Slow load | `slow_ms=4000` |
| App error | `app_error=true` |
| Needs a human | Deposit over $10,000 triggers the supervisor override |

Copy `faults.properties.example` to `faults.properties` in the directory you launch from.
The app re-reads it on every action.

## Deliberately hostile to automation

- No component names or test IDs.
- MDI internal frames and modal option dialogs.
- Open Sub-Account labels are not linked to their fields, so the accessibility tree
  exposes unnamed text fields. Locators must use nearby label text or position.
- Status messages appear only in a bottom status bar, in upper case, the way old
  cores report errors.
