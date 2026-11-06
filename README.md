# Tidepull
A small App(Open Source) for Downloading Youtube Videos on Windows, Paste a URL, pick a resolution, download. Optional signed-in session feature added for authentication related issues.
<img width="851" height="618" alt="Screenshot 2026-11-06 221155" src="https://github.com/user-attachments/assets/e30e0b58-fecc-4c15-9560-c137f5bf35be" />
## Features

- Resolution, frame-rate and audio-language selection
- Download speed cap
- Thumbnail, title, channel and duration preview
- Optional Google sign-in in an isolated browser profile
- Automatic fallback to a guest request if the signed-in one is starved
- Native yt-dlp format selection, so DASH is preferred over HLS and audio stays in sync

## Requirements
ffmpeg + ffprobe | `winget install Gyan.FFmpeg` <br>
JS runtime (deno ≥ 2.3.0) | `winget install DenoLand.Deno` <br>
Browser for sign-in | Edge (preinstalled) or Chrome

## Python Version

Tested Successfully on Python 3.12 and Python 3.15

## Download

**Windows (64-bit):** [Download Tidepull-win64.zip](https://github.com/pwright0712/Tidepull/releases/tag/v1.0.0)

1. Download the zip and **extract it** — do not run the exe from inside the archive. PyInstaller's one-folder build needs `_internal\` sitting next to `Tidepull.exe`.
2. Run `Tidepull.exe` from the extracted folder.
3. Requires Windows 10 or 11, 64-bit, and an internet connection.

## Signing in

Only needed for age-restricted, members-only, private or region-locked videos or authentication related issues. Guest downloads work without it.

**Tidepull uses its own browser profile, not your normal one.** Your existing
Chrome or Edge session is never read — being signed into YouTube in your everyday
browser does nothing here. The sign-in you do for Tidepull is separate, and may be
a different Google account.

#### Before you start

- Microsoft Edge or Google Chrome is installed (Edge ships with Windows)
- No Tidepull browser window is open from a previous attempt
- A fresh sign-in is required every time, the account picker alone is not enough

#### Steps

1. Click **google login** and confirm the prompt.
2. A separate browser window opens on a dedicated profile.
3. Sign in fully. Wait for YouTube to load a normal page — a spinner or the account
   chooser still on screen means it is not finished.
4. **Close that window completely.** Not minimise, not leave it in the background.
   Tidepull only detects the session once the window is gone.
5. The badge turns `authenticated` and the logout button enables.

If you close the window before finishing, nothing is saved and the badge stays
`guest`.

#### Signing out

Click **logout**, sign out of Google in the window, then close it. If you close
while still signed in, Tidepull asks whether to discard the saved session — saying
yes deletes the profile.

#### Where it lives

`%LocalAppData%\Tidepull\profile`

Deleting that folder is equivalent to logging out. Cookies are read from it locally
and never uploaded anywhere.

#### If detection fails

| Symptom | Cause | Fix |
|---|---|---|
| Badge stays `guest` after signing in | window still open, or sign-in not completed | close the window fully, sign in again |
| `Could not copy … cookie database` | browser still running on that profile | close it, retry |
| `browser not found` | no Chromium-family browser | install Edge or Chrome |

### Optional: reuse your existing browser session

Not implemented. If you want it, omit the profile argument in `_auth_opts` so
yt-dlp reads the browser's default profile, and keep the browser closed during
downloads. The trade-off is that logout can no longer just delete the profile.

Discord Contact Username: nukeexplosion1925
