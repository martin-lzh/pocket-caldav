# CalDAV Subscription Server

Single-file Python service that combines:

- Radicale for standards-compliant CalDAV serving.
- FastAPI management endpoints under `/api/*`.
- Filesystem-backed attachment storage with automatic cleanup.

It is designed for Apple Calendar and for Android devices that include a native CalDAV account provider. Many stock Android builds do not include native CalDAV; this server stays standards-compliant but cannot add missing OS sync-provider support on the phone.

## Quick Start on Windows

```powershell
cd D:\projects\caldav-subscription-server
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python server.py
```

The first run creates:

- `data\users.json` with generated CalDAV usernames and passwords.
- `data\api_key.txt` with a generated management API key.
- `data\collections` for Radicale calendar data.
- `data\attachments` for uploaded event attachments.

The generated credentials are printed on startup. For real deployment, set explicit environment variables instead of relying on generated development credentials.

## Configuration

PowerShell examples:

```powershell
$env:HOST = "0.0.0.0"
$env:PORT = "5232"
$env:PUBLIC_BASE_URL = "https://calendar.example.com"
$env:CALDAV_API_KEY = "replace-with-a-long-random-api-key"
$env:CALDAV_USERS = "work:replace-work-password,personal:replace-personal-password,shared:replace-shared-password,main:replace-main-password"
$env:ATTACHMENT_TTL_DAYS = "14"
python server.py
```

`CALDAV_USERS` can also be JSON:

```powershell
$env:CALDAV_USERS = '{"work":"work-password","personal":"personal-password","shared":"shared-password","main":"main-password"}'
```

Run a single process/worker for this script. Radicale and the management API share the same filesystem storage and coordinate with Radicale's storage lock.

## Calendar URLs

Local development base URL:

```text
http://127.0.0.1:5232
```

Separate-user layout:

```text
http://127.0.0.1:5232/work/default/
http://127.0.0.1:5232/personal/default/
http://127.0.0.1:5232/shared/default/
```

One-user slot layout:

```text
http://127.0.0.1:5232/main/work/
http://127.0.0.1:5232/main/personal/
http://127.0.0.1:5232/main/shared/
```

For Apple Calendar on iOS/macOS:

1. Open Calendar account settings.
2. Add a CalDAV account.
3. Server: `https://calendar.example.com` or `http://127.0.0.1:5232` for local testing.
4. Username/password: one of the users from `data\users.json` or `CALDAV_USERS`.
5. If manual URL entry is available, use one of the full calendar URLs above.

For Android:

1. Use the built-in CalDAV account provider if your Android/OEM build includes one.
2. Server: `https://calendar.example.com` or the full calendar URL.
3. Username/password: matching CalDAV credentials.

Use HTTPS in production. CalDAV uses Basic authentication here, so cleartext HTTP exposes credentials on the network.

## Management API

All `/api/*` calls require:

```text
X-API-Key: <value from CALDAV_API_KEY or data\api_key.txt>
```

Create a calendar:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:5232/api/calendars `
  -Headers @{ "X-API-Key" = "<api-key>" } `
  -ContentType "application/json" `
  -Body '{"owner":"main","calendar":"work","display_name":"Work"}'
```

Create or update an event:

```powershell
$ics = @"
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Local CalDAV Bridge//EN
BEGIN:VEVENT
UID:event-001
DTSTAMP:20260526T090000Z
DTSTART:20260527T100000Z
DTEND:20260527T110000Z
SUMMARY:Example Event
END:VEVENT
END:VCALENDAR
"@

Invoke-RestMethod `
  -Method Put `
  -Uri http://127.0.0.1:5232/api/calendars/main/work/event-001 `
  -Headers @{ "X-API-Key" = "<api-key>" } `
  -ContentType "text/calendar" `
  -Body $ics
```

Upload an attachment:

```powershell
curl.exe `
  -X POST `
  -H "X-API-Key: <api-key>" `
  -F "file=@C:\path\to\file.pdf" `
  http://127.0.0.1:5232/api/calendars/main/work/event-001/attachments
```

Delete an event:

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri http://127.0.0.1:5232/api/calendars/main/work/event-001 `
  -Headers @{ "X-API-Key" = "<api-key>" }
```

Manually run attachment cleanup:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:5232/api/cleanup `
  -Headers @{ "X-API-Key" = "<api-key>" }
```

## Production Notes

- Put the service behind an HTTPS reverse proxy such as Caddy, nginx, or IIS ARR.
- Set `PUBLIC_BASE_URL` to the external HTTPS origin so event attachment URLs are usable from clients.
- Store `data` somewhere backed up and protected by filesystem permissions.
- Keep `CALDAV_API_KEY` and CalDAV passwords secret.
- Start with one worker process. If you need multi-process scaling, move management writes behind a single writer or use Radicale-native operations instead of direct filesystem writes.
