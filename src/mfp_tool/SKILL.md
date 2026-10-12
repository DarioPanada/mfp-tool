---
name: mfp
description: Read and log Dario's MyFitnessPal food diary with the `mfp` command. Use when he asks what he ate, about calories, macros (carbs, fat, protein) or a day's meals, asks to log or remove a food, or mentions MyFitnessPal or MFP.
---

# MyFitnessPal (`mfp`)

`mfp` is a command-line client for MyFitnessPal, installed from github.com/DarioPanada/mfp-tool. It logs in with a session cookie saved from Dario's browser, so there is no password or MCP server involved.

## Commands

```sh
mfp session              # check the login (also keeps the cookie fresh)
mfp diary [YYYY-MM-DD]   # food entries for a day, default today, as a JSON array
mfp search <text> [--max N] [--page P] [--country CC]   # foods, as JSON
mfp log <food id> <food version> --meal <name|position> --servings N \
    --value V --unit U --multiplier M [--date YYYY-MM-DD]  # prints the new entry
mfp delete <entry id>
```

Exit status: 0 ok, 1 error (message on stderr), **2 the login has expired**.

## Reading the diary

Each entry has:

- `meal_name` (Breakfast, Lunch, Dinner, Snack 1, …)
- `food.description` and `food.brand_name`
- `servings` and `serving_size` (`value`, `unit`)
- `nutritional_contents`: `energy.value` (kcal), `carbohydrates`, `fat`, `protein`, `sugar`, `fiber`, `sodium` (grams, sodium in mg)

Any nutrient can be `null`: count it as 0. Answer with a short table grouped by meal, then the day's totals. Parse the JSON with a small script rather than printing all of it; it is long.

## Logging food

1. `mfp search` the food. Each result has `id`, `version`, `description`, `brand`, `kcal` (for a
   `nutrition_multiplier` of 1) and `serving_sizes` (`value`, `unit`, `nutrition_multiplier`).
2. Pick the serving size and work out the servings. The entry's kcal is
   `kcal × nutrition_multiplier × servings`.
3. **Always ask Dario to confirm before running `mfp log`**, every time, even when he asked for the
   food by name. Show the food and brand, serving size, servings, meal, date and kcal, then wait for
   a clear yes. Do not log anything he hasn't confirmed, and do not batch confirmations he hasn't seen.
4. `--meal` takes a meal name as it appears in the diary (looked up in today's diary and in the
   target day's), or its position. A meal with no entries on those days isn't found by name; give
   its position instead: 0 Breakfast, 1 Snack 1, 2 Lunch, 3 Snack 2, 4 Dinner (check an earlier
   `mfp diary`, whose entries carry `meal_name` and `meal_position`, if unsure).
5. `mfp log` prints a JSON list holding the new entry. Report its `id`.

`mfp delete <entry id>` removes an entry (its `id` from `mfp diary` or `mfp log`). Confirm with
Dario before deleting too, naming the entry.

## Rules

- **Never read, print, copy or log `~/.config/mfp/session-token`** or any cookie value, not even partly, and not while debugging. Only `mfp` touches it.
- Exit status 2 means the session has expired. Don't try to log in. Ask Dario to save a fresh cookie, following "The session cookie" in the mfp-tool README.
- Only change the diary with `mfp log` and `mfp delete`, and only after Dario confirms. Don't call MFP's API another way to add or change entries.
- Diary data is personal. Never commit it to a repository, including mfp-tool, which is public.
