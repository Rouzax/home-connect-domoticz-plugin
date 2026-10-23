# Installation

Installing takes three parts: an application on the Home Connect developer portal (once), the
plugin in Domoticz, and signing in with your Home Connect account.

## Before you start

You need:

- **Your appliances in the Home Connect app.** The plugin sees exactly the appliances on your
  Home Connect account. Add and connect them in the app first.
- **Domoticz with Python plugin support.** Version 2026.4 or newer is recommended: older
  versions show plugin password fields, such as the Client Secret, to users who are not an
  administrator. The plugin works on older versions and logs a warning at start.
- **Python 3.11 or newer** on the machine that runs Domoticz. On an older Python the plugin
  stops at start and says why in the log.
- **Internet access** from the Domoticz machine to `api.home-connect.com`.
- **"Accept new Hardware Devices" switched on** in Domoticz (Setup > Settings > System). It is
  on by default; while it is off, Domoticz refuses the plugin's devices.

## 1. Register an application at Home Connect

The plugin talks to Home Connect as an "application" that you register yourself. This is free
and takes a few minutes.

1. Go to [developer.home-connect.com](https://developer.home-connect.com) and create an
   account. **Use the same email address as your Home Connect app account.** Home Connect only
   lets an application sign in to accounts whose email matches the developer account; with a
   different address, signing in later fails with "The given user is not assigned to this app".
2. Accept the terms of use when the portal asks for it.
3. Under **Applications**, register a new application:
    - **OAuth Flow:** Device Flow.
    - **Redirect URI:** the form requires one. After you approve the sign-in, your browser is
      sent there, and nothing else uses it. Enter any address, for example
      `https://localhost`; the browser then shows an error page after approving, which is
      harmless.
    - Name and other fields: anything you like.
4. Open the application and copy its **Client ID**. The portal also shows a **Client Secret**.
   The plugin works without it, so you can leave it out; see
   [Settings](settings.md#client-secret).

## 2. Install the plugin

Clone the `dist` branch into the Domoticz plugins folder. The `dist` branch holds only the files
Domoticz needs.

```bash
cd /opt/domoticz/plugins     # your Domoticz plugins folder
git clone -b dist https://github.com/Rouzax/home-connect-domoticz-plugin homeconnect
```

The plugins folder depends on how Domoticz is installed: `domoticz/plugins` next to the
Domoticz program, or `/opt/domoticz/userdata/plugins` in the official Docker image.

Without git, download the zip attached to the newest release on the
[releases page](https://github.com/Rouzax/home-connect-domoticz-plugin/releases) instead and
unpack it in the plugins folder: it contains the same files in a `homeconnect` folder. To update,
replace that folder with the one from a newer zip.

Restart Domoticz so it finds the new plugin.

## 3. Add the hardware

1. In Domoticz, go to **Setup > Hardware**.
2. Choose **Home Connect** as the type, give it a name (for example "Home Connect").
3. Paste your **Client ID**. Client Secret is optional; leaving it empty works.
4. Leave the other settings as they are for now; [Settings](settings.md) explains each one.
5. Press **Add**.

## 4. Sign in

Right after you add the hardware, the plugin asks Home Connect for a sign-in code.

1. Open the **Utility** tab. The **Home Connect** device shows a link "Sign in to Home Connect"
   and a code, with the time until which the code is valid. The same link and code are in the
   Domoticz log (Setup > Log).
2. Open the link. It already contains the code; check that the page shows the same code.
3. Log in with your Home Connect account (your SingleKey ID) and approve access. The browser
   then goes to the redirect address you registered; with `https://localhost` that is an error
   page, which you can close.

Within about ten seconds the plugin is signed in. It then reads your appliances and creates their
devices. The **Home Connect** device is hidden once you are signed in; you find it under
Setup > Devices if you want to check the sign-in status later.

If the code expires before you approve it, the device says so. Choose **Sign in again** on the
**Home Connect actions** device next to it to get a new link and code (pressing **Update** in
the hardware settings does the same).

## What happens next

- Each appliance gets a set of devices: state, program, remaining time and more. Devices for
  values most people never need are created hidden. [Devices](devices.md) lists them.
- The plugin keeps a live connection to Home Connect and updates devices as the appliances
  report changes. It does not poll every few seconds, so it stays well within Home Connect's
  daily request limit.
- Nothing is sent to your appliances. Control stays off until you turn on Allow control.

## Updating

```bash
cd /opt/domoticz/plugins/homeconnect
git pull
```

Restart Domoticz afterwards. Your devices, their history and your sign-in are kept.

## Removing

1. Turn on **Sign out** in the hardware settings and press **Update**. The plugin withdraws its
   access at Home Connect, so no unused sign-in stays on your account.
2. Delete the hardware in Setup > Hardware. Domoticz removes its devices.
3. Delete the `homeconnect` folder from the plugins folder.
