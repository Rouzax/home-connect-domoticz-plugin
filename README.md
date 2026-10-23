# Home Connect for Domoticz

![Home Connect for Domoticz: a running washer and dryer as Domoticz devices](https://raw.githubusercontent.com/Rouzax/home-connect-domoticz-plugin/main/docs/assets/hero.png)

Your Bosch, Siemens, Neff and Gaggenau appliances from the Home Connect cloud in Domoticz:
state, program, remaining time, settings, messages and, if you allow it, control.

> **Beta.** This is the first public release. It runs daily on a real household of appliances,
> but expect rough edges, and report what you find.

> **Documentation, install guide, settings and troubleshooting:**
> **[rouzax.github.io/home-connect-domoticz-plugin](https://rouzax.github.io/home-connect-domoticz-plugin/)**

## What you get

| Feature | What it does |
|---------|--------------|
| **Every appliance on your account** | Washers, dryers, dishwashers, ovens, microwaves, fridges and more appear on their own, with a device per value they report. |
| **Live state** | Operation state, door, program, progress, remaining time and the finish time, updated from Home Connect's event stream as they change. |
| **Status code for scripts** | One selector with fixed levels per state, so scripts never depend on translated text. |
| **Alerts** | Appliance messages (program finished, rinse aid nearly empty, preheat finished) as Domoticz alerts. |
| **Washer meters** | Lifetime energy, water, detergent and softener as Domoticz meters, with use per day, month and year. |
| **Opt-in control** | Choose a program, change options, start, stop, pause and resume, or start later at a chosen time. Off unless you turn on Allow control. |
| **English and Nederlands** | Device names and values in either language. |

## Requirements

- Domoticz with Python plugin support; 2026.4 or newer is recommended (older versions show the
  Client Secret to non-admin users).
- Python 3.11 or newer on the Domoticz host.
- A free Home Connect developer account, registered with the same email address as your Home
  Connect app, and an application of type Device Flow. The documentation walks you through it.
- Internet access from the Domoticz host.

## Install (short)

Clone the lean **`dist`** branch into your Domoticz plugins directory, then restart Domoticz:

```bash
cd /opt/domoticz/plugins            # adjust to your install path
git clone -b dist https://github.com/Rouzax/home-connect-domoticz-plugin homeconnect
```

Then: **Setup > Hardware > Add > Home Connect**, enter your Client ID and press **Add**. Open
the **Home Connect** device on the Utility tab, follow its link and approve with your Home
Connect account. To update later: `git pull` in that folder and restart Domoticz.

Every step, every setting and what each device means are in the
**[documentation](https://rouzax.github.io/home-connect-domoticz-plugin/)**.

## Security

The Home Connect tokens (and the Client Secret, if you enter one) are stored in the Domoticz
database in cleartext, so treat database backups as secrets. Control is off by default. Sign
out withdraws the plugin's access at Home Connect. See the
[Security page](https://rouzax.github.io/home-connect-domoticz-plugin/security/).

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](https://github.com/Rouzax/home-connect-domoticz-plugin/blob/main/CONTRIBUTING.md) for the
development setup and checks.

## License

MIT, see [LICENSE](LICENSE). Home Connect is a trademark of BSH Hausgeraete GmbH; this project is not
affiliated with BSH.
