# Security

What the plugin stores, what that gives access to, and how to limit it.

## What is stored, and where

- **The Home Connect sign-in tokens** are stored in the Domoticz database, in plain text, in the
  plugin's own configuration. So is the **Client Secret** if you entered one.
- The tokens give **control over your appliances**, also when Allow control is off: that setting
  stops the plugin from sending commands, but someone who copies the tokens out of the database
  could use them directly.
- **Treat the Domoticz database and its backups as secrets.** Anyone with a copy can use your
  Home Connect sign-in until you withdraw it.

## What the plugin connects to

- **Home Connect** (`api.home-connect.com`): every request is an outgoing HTTPS connection from
  the Domoticz machine. Nothing connects in, so no port forwarding is involved.
- **GitHub** (`api.github.com`), once a day, for the [update check](settings.md#update-check):
  one request for the list of the plugin's releases. It carries no data about you, your
  Domoticz or your appliances. Set Update check to Off to stop it.

## Withdrawing access

- **Sign out** in the hardware settings withdraws the plugin's access at Home Connect and clears
  the tokens from the database ([Settings](settings.md#sign-out)).
- You can also remove the plugin's access in your Home Connect account yourself. Do this when
  a backup went somewhere it should not, or when you replaced the Client ID: the plugin cannot
  withdraw an old application's access itself.
- Unused tokens expire after 60 days; while the plugin runs, it renews them daily.

## Who can see what in Domoticz

- **Domoticz older than 2026.4** shows plugin password fields, here the Client Secret, to users
  who are not an administrator. Use 2026.4 or newer, or leave the Client Secret empty (the plugin
  works without it).
- The **Home Connect actions** device (Setup > Devices once you are signed in) can start a new
  sign-in. A new sign-in only replaces the
  current one when someone approves it with your Home Connect account, so the worst a misuse
  can do is put a new code in the log. To be safe, set the device to **Protected** (in its edit
  dialog) so it asks for the Domoticz PIN.
- Device IDs contain each appliance's Home Connect ID, which for many brands includes its model
  and serial number. Keep that in mind when you share screenshots or device lists.

## What the log contains

- The log never contains tokens, the Client Secret or the sign-in code's secret part. The short
  user code and link are logged on purpose while you need to sign in.
- Appliances are named in the log by their name, not by their ID.
- With Debug level Verbose, every value Home Connect sends is logged. That shows what your
  appliances do and when; keep Verbose for troubleshooting.
