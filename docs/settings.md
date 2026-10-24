# Settings

All settings are on the hardware entry: **Setup > Hardware**, select the Home Connect hardware,
change what you need and press **Update**. Pressing Update restarts the plugin: it reconnects
to Home Connect within a few seconds and keeps your sign-in and devices.

| Setting | Default | What it does |
|---------|---------|--------------|
| [Client ID](#client-id) | (required) | The application you registered at Home Connect. |
| [Client Secret](#client-secret) | empty | Optional; the plugin works without it. |
| [Allow control](#allow-control) | off | Lets Domoticz operate your appliances. |
| [Device language](#device-language) | Same as Domoticz | Language of device names and values. |
| [On-screen messages](#on-screen-messages) | Problems | What a command shows on screen. |
| [Update check](#update-check) | Automatic | Whether the plugin tells you about a newer version. |
| [Sign out](#sign-out) | off | Withdraws the plugin's access at Home Connect. |
| [Debug level](#debug-level) | None | How much the plugin writes to the Domoticz log. |

## Client ID

The Client ID of your application on the Home Connect developer portal
([Installation, step 1](install.md#1-register-an-application-at-home-connect)).

If you change it to another application, the plugin treats it as a new sign-in: it forgets the
old sign-in and shows a new link and code. It cannot withdraw the old application's access
(Home Connect only accepts that from the old application itself), so it logs a reminder to
remove that access in your Home Connect account.

## Client Secret

Optional. The developer portal shows a secret for your application, but the plugin signs in
without it. If you enter it, the plugin sends it when it signs in and renews its sign-in, as
Home Connect recommends. Like the tokens, it is stored in the Domoticz database in plain text
([Security](security.md)); leaving it empty is a reasonable choice.

## Allow control

Off by default. While it is off, the plugin only reads from Home Connect:

- every command to an appliance is refused, and the Domoticz log says why ("'Allow control' is
  off in the hardware settings");
- the Actions, Acknowledge and Start at / Ready at devices are hidden; Program choice stays
  visible but does not send anything.

Turn it on to choose programs, change options and settings, and start, stop, pause or resume
appliances from Domoticz, including from scenes, timers and scripts. A script or a misclick can
then start a program. An appliance only accepts a start while remote start is switched on at the
appliance itself, so nothing starts without that step at the appliance. See
[Control](control.md).

## Device language

**Same as Domoticz** (default) uses Dutch when the Domoticz language is Dutch, and English
otherwise. You can also choose **English** or **Nederlands** directly.

The language applies to device names, values (program names, states) and the plugin's messages.
What changes when you switch it:

- Device names and values follow the new language, except names you changed yourself: those
  are kept. A device for an appliance message (an alert) changes when the appliance next
  reports that message, so it can stay in the old language for a while.
- Device descriptions keep the language they were created in.

## On-screen messages

When a command is given (Start, a program, an option), from the screen or by a scene, timer or
script, the plugin shows its result as a short message at the top of the Domoticz screen, for
example "Dishwasher Actions: not sent / The door of Dishwasher is not closed". Each message says
what went wrong and what to do next.

- **Problems** (default): commands that were refused, failed, could not be sent in time, or
  were not confirmed by the appliance.
- **Problems and confirmations**: also a message when the appliance confirmed a command.
- **Off**: no messages on screen.

Good to know:

- The message appears in every browser that has Domoticz open at that moment, not only yours,
  and disappears after a few seconds.
- It needs Domoticz 2026.1 or newer. On an older Domoticz nothing appears on screen.
- It only goes to the screen: it is not sent by email, Telegram or any other notification
  system you set up in Domoticz.
- Whatever you choose, the Domoticz log always has the result of every command.

## Update check

A few minutes after it starts, and then once a day, the plugin asks GitHub whether a newer
version of the plugin exists. When there is one, it tells you once per version:

- a line in the Domoticz log (`update_available`, with the version and a link to its release
  notes);
- a notification through every notification system you set up in Domoticz (for example email,
  Telegram or a push app), which also shows briefly on screen.

The plugin does not update itself: you update as you installed it
([Installation](install.md#updating)).

- **Automatic** (default): while you run a beta, it reports betas and stable releases; while you
  run a stable release, only stable releases.
- **Stable releases only** or **Including betas**: choose for yourself.
- **Off**: no check at all.

The check is one request to `api.github.com`. It sends nothing about you, your Domoticz or your
appliances, and it does not count towards the Home Connect daily limit. When GitHub cannot be
reached, the plugin tries again the next day.

## Sign out

Turn it on and press **Update** to sign out. The plugin:

1. forgets its sign-in at once and stops updating devices (the devices and their history stay);
2. asks Home Connect to withdraw its access. If that request fails, the log tells you to remove
   the access in your Home Connect account yourself;
3. stays signed out, also after a restart.

To sign in again, turn Sign out off and press **Update**. A new link and code appear on the
Home Connect device ([Installation, step 4](install.md#4-sign-in)).

## Debug level

- **None** (default): normal use. Sign-in, warnings, errors and the outcome of each command you
  give are logged.
- **Basic**: also one line per request to Home Connect, with how long it took.
- **Verbose**: also every message Home Connect sends, item by item. This fills the log quickly;
  use it only while looking into a problem.

The log never contains your sign-in tokens or Client Secret.
