---
name: mfp
description: Read Dario's MyFitnessPal food diary with the `mfp` command. Use when he asks what he ate, about calories, macros (carbs, fat, protein) or a day's meals, or mentions MyFitnessPal or MFP.
---

# MyFitnessPal (`mfp`)

`mfp` is a command-line client for MyFitnessPal, installed from github.com/DarioPanada/mfp-tool. It logs in with a session cookie saved from Dario's browser, so there is no password or MCP server involved.

## Commands

```sh
mfp session              # check the login (also keeps the cookie fresh)
mfp diary [YYYY-MM-DD]   # food entries for a day, default today, as a JSON array
```

Exit status: 0 ok, 1 error (message on stderr), **2 the login has expired**.

## Reading the diary

Each entry has:

- `meal_name` (Breakfast, Lunch, Dinner, Snack 1, …)
- `food.description` and `food.brand_name`
- `servings` and `serving_size` (`value`, `unit`)
- `nutritional_contents`: `energy.value` (kcal), `carbohydrates`, `fat`, `protein`, `sugar`, `fiber`, `sodium` (grams, sodium in mg)

Any nutrient can be `null`: count it as 0. Answer with a short table grouped by meal, then the day's totals. Parse the JSON with a small script rather than printing all of it; it is long.

## Rules

- **Never read, print, copy or log `~/.config/mfp/session-token`** or any cookie value, not even partly, and not while debugging. Only `mfp` touches it.
- Exit status 2 means the session has expired. Don't try to log in. Ask Dario to save a fresh cookie, following "The session cookie" in the mfp-tool README.
- It is read-only for now: there is no command to log food yet. Don't call MFP's API another way to add or change entries.
- Diary data is personal. Never commit it to a repository, including mfp-tool, which is public.
