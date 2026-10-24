# How it works

This page is for the curious and for troubleshooting. You do not need it to use the plugin.

## The event stream

The plugin keeps one open connection to Home Connect for all your appliances: the event stream.
Home Connect sends a message over it whenever an appliance changes (state, door, remaining
time, a new message), and the plugin updates the matching device within seconds. Because of the
stream the plugin does not poll: an idle household costs only a few requests per appliance a
day, for renewing the sign-in and a daily check of each appliance's programs and commands.

Home Connect closes the stream now and then (in testing, once a day, 24 hours after signing
in). The plugin reconnects by itself, waiting a little longer after each failed attempt.
While it has no stream for more than 5 minutes, State shows "(last known)".

## Requests and the daily limit

Home Connect allows 1000 requests per day per account. The plugin counts every request it makes
over a rolling 24 hours and stays below that with a margin:

- It stops at 950 requests in 24 hours.
- Each kind of work has its own ceiling, so one kind cannot use up the day: sign-in and token
  renewal, reconnecting the stream, and background reading (appliance lists, program details,
  settings) each have a share.
- Commands you give may use what is left, except the last 70 requests of the day, which stay
  free for renewing the sign-in and reconnecting the stream. A day of busy automations can
  therefore never sign the plugin out.
- At most 30 requests a minute, and at most 5 program starts and 5 stops a minute.

The **Home Connect API calls 24h** device (hidden, under Setup > Devices) shows the count.

What it costs in practice:

- A **first install** reads every appliance, its programs and their details once: about 100
  requests for a household of five appliances. Program details are kept, so this happens once.
- A **restart** costs about 20 requests.
- A **day of normal use** costs a few dozen requests (in testing, about 35 for a day with seven
  program runs); an idle day costs a few per appliance.

The daily [update check](settings.md#update-check) asks GitHub, not Home Connect, so it is not
counted.

## When Home Connect says no

- **Too many requests (429):** Home Connect asks the plugin to wait. The plugin stops all
  requests for the time Home Connect names, and remembers it across a restart, so a restart
  does not make it worse.
- **Repeated errors:** after several errors in a short time, the plugin pauses its requests for
  a while instead of trying again and again.
- **An appliance that is offline** answers with an error; the plugin shows it as Offline and
  waits for the appliance to come back, which the stream reports.

## Sign-in

The plugin signs in with the OAuth device flow: it gets a code from Home Connect, you approve it
in the browser, and the plugin receives tokens. It renews them by itself before they expire. You
only sign in again when the sign-in is lost: when you remove the plugin's access in your Home
Connect account, or when the plugin could not reach Home Connect for longer than the renewal
token lasts (60 days).
