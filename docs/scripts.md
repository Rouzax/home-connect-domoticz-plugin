# Script examples

The plugin's devices work in dzVents and Lua scripts like any other Domoticz device. For
anything that reacts to an appliance's state, trigger on the Status code device: its levels
never change and do not depend on the device language
([Status code for scripts](devices.md#status-code-for-scripts)).

## Notify when an appliance has finished

This dzVents script follows one or more appliances and sends one notification per program:

- "Cotton is finished" when a program ends normally;
- for a dryer: "Cotton is dry" as soon as the laundry is dry, before the wrinkle guard
  (anti-crease) starts, and nothing more when the program ends later;
- "Cotton was aborted" when the program was aborted.

The message body says how long the program took, for example "Took 2 h 14 min (12:45 - 14:59)".
It goes through Domoticz's own notification system, so it arrives wherever you set that up
(email, Telegram, a push app); see [Update check](settings.md#update-check) for the checkboxes
that decide which systems receive notifications.

### What you need

- The **Status code** device of each appliance, set as used. A new installation creates it used,
  named with a leading "$" (for example "$Washer Status code"). If it is not used (an install
  from before 0.1.0-beta.3), tick it under **Setup > Devices**; scripts do not see a device that
  is not used.
- Optional, per appliance: its **Program** text device, for the program name in the message,
  and its **Program aborted** and (dryer) **Drying finished** alert devices. An alert device only
  exists after the appliance sent that message for the first time; add its idx later, the script
  works without it.

### Set it up

1. Look up the idx numbers under **Setup > Devices**: filter on the Home Connect hardware.
2. Open **Setup > More Options > Events**, add a new **dzVents** script of type **Device**,
   replace its contents with the script below and save it.
3. Fill in `APPLIANCES` at the top: one line per appliance, with the idx numbers. `name` is used
   in the message when the Program device shows no program, and must be different per line.

A program that is already running when you save the script is not reported; the next one is.

```lua
--[[
Home Connect: notify when an appliance has finished

A run starts when the Status code goes to Running (40). It survives Paused,
Action required and Offline, and ends at Finished (70), Ready (20), Inactive
(10) or Delayed start (30): a timed dryer program, for example, ends at Ready
without passing Finished. The Program aborted alert can arrive in the same
second as that change, in either order, so the end is decided a few seconds
later. Runs shorter than MIN_RUNTIME are ignored (a program started by mistake,
or a dryer that finds the laundry already dry).
--]]

local APPLIANCES = {
    -- name: used when the Program device shows no program
    -- status: the "$... Status code" selector (required)
    -- program: the "... Program" text device (optional)
    -- dryingFinished, programAborted: alert devices (optional)
    { name = 'Washer', status = 0, program = 0, programAborted = nil },
    { name = 'Dryer', status = 0, program = 0, dryingFinished = nil, programAborted = nil },
}

local MIN_RUNTIME = 300     -- seconds
local END_DELAY = 10        -- seconds after the end before deciding finished or aborted
local END_EVENT = 'homeConnectRunEnd'

local TEXT = {
    FINISHED = '%s is finished',
    DRY = '%s is dry',
    ABORTED = '%s was aborted',
    DURATION = 'Took %s (%s - %s)',
}

-- Status code levels of the Home Connect plugin (a fixed contract).
local STATUS = {
    INACTIVE = 10, READY = 20, DELAYED_START = 30, RUNNING = 40, PAUSED = 50,
    ACTION_REQUIRED = 60, FINISHED = 70, ABORTING = 80, ERROR = 90, OFFLINE = 100,
}
local ENDS_RUN = { [10] = true, [20] = true, [30] = true, [70] = true }

local BY_IDX = {}
local TRIGGERS = {}
for _, a in ipairs(APPLIANCES) do
    for _, key in ipairs({ 'status', 'dryingFinished', 'programAborted' }) do
        if a[key] and a[key] > 0 then
            BY_IDX[a[key]] = { appliance = a, role = key }
            table.insert(TRIGGERS, a[key])
        end
    end
end

local function minutes(seconds)
    local m = math.floor(seconds / 60 + 0.5)
    if m < 60 then return string.format('%d min', m) end
    return string.format('%d h %d min', math.floor(m / 60), m % 60)
end

-- "Cotton, ready at about 14:59", "Cotton (selected)" or "-" -> "Cotton" or nil.
local function programName(domoticz, a)
    local dev = a.program and a.program > 0 and domoticz.devices(a.program)
    local text = dev and dev.text
    if not text or text == '' or text == '-' then return nil end
    return text:match('^%s*([^,(]-)%s*[,(]') or text
end

-- An alert device is grey (0) without a message, whatever the language.
local function alertOn(domoticz, idx)
    local dev = idx and domoticz.devices(idx)
    return dev and (dev.color or 0) > 0 or false
end

local function notify(domoticz, a, run, template, upTo)
    local subject = string.format(template, run.program or a.name)
    local body = string.format(TEXT.DURATION, minutes(upTo - run.start),
        os.date('%H:%M', run.start), os.date('%H:%M', upTo))
    domoticz.notify(subject, body, domoticz.PRIORITY_NORMAL)
    domoticz.log(subject .. '. ' .. body, domoticz.LOG_INFO)
end

return {
    on = {
        devices = TRIGGERS,
        customEvents = { END_EVENT },
    },
    data = {
        runs = { initial = {} },   -- appliance name -> run in progress or ending
    },
    logging = { marker = 'home-connect-done' },

    execute = function(domoticz, item)
        local runs = domoticz.data.runs
        local now = domoticz.time.dDate

        if item.isCustomEvent then
            local name, stamp = tostring(item.data):match('^(.*)|(%d+)$')
            local run = name and runs[name]
            if not run or tostring(run.ended) ~= stamp then return end
            runs[name] = nil
            for _, a in ipairs(APPLIANCES) do
                if a.name == name then
                    if run.ended - run.start < MIN_RUNTIME then
                        domoticz.log(name .. ': short run ignored', domoticz.LOG_INFO)
                    elseif run.aborted then
                        notify(domoticz, a, run, TEXT.ABORTED, run.ended)
                    elseif not run.notified then
                        notify(domoticz, a, run, TEXT.FINISHED, run.ended)
                    end
                end
            end
            return
        end

        local entry = BY_IDX[item.idx]
        if not entry then return end
        local a = entry.appliance
        local run = runs[a.name]

        if entry.role == 'programAborted' then
            if run and alertOn(domoticz, a.programAborted) then run.aborted = true end
            return
        end

        if entry.role == 'dryingFinished' then
            if run and not run.ended and not run.notified and alertOn(domoticz, a.dryingFinished)
                and now - run.start >= MIN_RUNTIME then
                run.notified = true
                notify(domoticz, a, run, TEXT.DRY, now)
            end
            return
        end

        local level = item.level
        if level == STATUS.RUNNING then
            if not run then
                runs[a.name] = { start = now, program = programName(domoticz, a) }
            elseif not run.program then
                run.program = programName(domoticz, a)
            end
        elseif level == STATUS.ABORTING or level == STATUS.ERROR then
            if run then run.aborted = true end
        elseif ENDS_RUN[level] and run and not run.ended then
            run.ended = now
            run.program = run.program or programName(domoticz, a)
            domoticz.emitEvent(END_EVENT, a.name .. '|' .. now).afterSec(END_DELAY)
        end
    end,
}
```

### How it decides

- A program counts from the moment the Status code goes to Running (40). Paused, Action
  required and Offline during the program do not end it.
- It ends at Finished (70), Ready (20), Inactive (10) or Delayed start (30). Not every program
  passes Finished: a timed dryer program, for example, goes straight from Running to Ready.
- Two programs back to back (a cotton program, then a refresh started a minute later) give two
  messages.
- The Program aborted alert can arrive in the same second as the end, before or after it, so
  the script waits `END_DELAY` seconds before it decides between "finished" and "aborted".
- Programs shorter than `MIN_RUNTIME` (5 minutes) are ignored: a program started by mistake, or
  a dryer that found the laundry already dry.
- The message texts are in `TEXT`; translate them there. The program name comes from the
  Program device, in the plugin's device language.

### Extending it: use per program

A washing machine reports its lifetime totals (energy, water, detergent, softener) in the same
seconds as Finished, so their meters may not have the new value yet when the program ends. To
add the use of one program to the message, store the totals when the program starts and read
them again about a minute after the end, for example with a second custom event and
`afterSec(60)`. The meters themselves are described under
[Washing machine meters](devices.md#washing-machine-meters).
