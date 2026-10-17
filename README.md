# App Timer

Tracks how long your laptop is in use and how long you spend in each app, on Windows.
You only need Python. No extra packages.

## Start it
Double-click **`Start App Timer.bat`** (or `app_timer.pyw`).
Closing the window only minimizes it, so tracking keeps going. Use **Quit** to stop it.
To start it every time you sign in, turn on **Settings → Start App Timer automatically**.

## What it tracks
- **Laptop screen time**: counts while the laptop is awake and unlocked. Sleep, the lock screen and shutdown don't count.
- **Per-app time** (Today tab):
  - *Normal apps* count the whole time they are open, whether in front or in the background. They never remind you.
  - *Watched apps* count only while they are **in front**. If one sits in the background (minimized or behind
    another window) longer than the time you set, a popup reminds you to go back to it. The popup has
    **Go back**, **Snooze 10 min** and **Dismiss** buttons.
- **History** tab: screen time for the last 14 days and your top apps for the last 7 days.

## Watch an app
Select it on the Today tab and click **Watch selected app**. You can also go to the **Watched apps** tab,
pick the app, set how many minutes it can stay in the background before it reminds you, and click **Save**.

Data is saved in `%APPDATA%\AppTimer\data.json`.
