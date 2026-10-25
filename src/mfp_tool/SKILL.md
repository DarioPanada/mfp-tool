---
name: mfp
description: Read and log Dario's MyFitnessPal diary with the `mfp` command. Use when he asks what he ate or burned, about calories, macros (carbs, fat, protein) or a day's meals, asks to log or remove a food, a quick add, exercise, a recipe or a weigh-in, asks about his weight, or mentions MyFitnessPal or MFP.
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
mfp delete <entry id>    # any diary entry: food, quick add or exercise
mfp quick-add --kcal N --meal <name|position> [--carbs G] [--fat G] [--protein G] [--date YYYY-MM-DD]
mfp exercises [YYYY-MM-DD]             # exercise entries for a day, as JSON
mfp exercise-search <text>             # exercises: id and description
mfp exercise-log <exercise id> --minutes N --kcal K [--date YYYY-MM-DD] [--time HH:MM]
mfp meals                              # saved meals: each food's name and nutrition
mfp foods [text]                       # foods Dario created (his recipes among them)
mfp food-create <description> --kcal K --value V --unit U [--brand B] [--carbs G] [--fat G] \
    [--protein G] [--sugar G] [--fiber G] [--sodium MG]   # prints the new food with id and version
mfp food-delete <food id>
mfp weights [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--limit N]   # weigh-ins, newest first
mfp weight-log <weight> --unit kg|lb [--date YYYY-MM-DD]       # save a weigh-in
mfp weight-delete <measurement id>
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

`mfp delete <entry id>` removes an entry (its `id` from `mfp diary`, `mfp exercises` or the command
that logged it). Confirm with Dario before deleting too, naming the entry.

## Quick add, saved meals

`mfp quick-add` logs calories (and any macros) to a meal without a food: for something not in the
database, or a total Dario gives you. `--meal` is a name from his diary settings or its position.
`mfp meals` lists his saved meals with each food's name and nutrition, but no food ids: log a saved
meal as one quick add of its totals (name the meal when you confirm), or search and log its foods.

## Exercise

1. `mfp exercise-search` the activity, and pick the closest description.
2. Calories burned: use the number Dario gives (from his watch, say). If he gives none, estimate it,
   say it is an estimate, and let him correct it.
3. Confirm the exercise, minutes, calories, date and start time, then `mfp exercise-log`. `--time` is
   his local start time; without it the entry starts now (12:00 on another day).

`mfp exercises` reads a day's exercise; `energy.value` is the calories burned, `duration` is seconds.

## Recipes

MFP's own recipe box isn't reachable, so a recipe is saved as one of Dario's foods:

1. Work out each ingredient's nutrition (`mfp search`), add them up, and divide by the servings.
2. Confirm the name (say "Chili (homemade)"), servings and per-serving numbers, then
   `mfp food-create "<name>" --brand Homemade --value 1 --unit serving --kcal ... --carbs ... --fat ... --protein ...`.
3. Log it like any food: `mfp log <id> <version> --value 1 --unit serving --multiplier 1 --servings N`.

Check `mfp foods` first: the recipe may already exist. Ask before `mfp food-delete`.

## Weight

`mfp weights` lists weigh-ins, newest first (30 unless `--limit`): each has an `id`, a `date`, a
`value` and its `unit`. For a trend, ask for a date range and summarise it (first, last, change)
rather than printing every entry.

To save one, use the number and unit Dario gives; if he gives no unit, ask. Confirm the weight, unit
and date, then `mfp weight-log`. Ask before `mfp weight-delete` too, naming the date and weight.

## Rules

- **Never read, print, copy or log `~/.config/mfp/session-token`** or any cookie value, not even partly, and not while debugging. Only `mfp` touches it.
- Exit status 2 means the session has expired. Don't try to log in. Ask Dario to save a fresh cookie, following "The session cookie" in the mfp-tool README.
- Only change the diary, his foods or his weigh-ins with `mfp log`, `quick-add`, `exercise-log`, `food-create`, `delete`, `food-delete`, `weight-log` and `weight-delete`, and only after Dario confirms. Don't call MFP's API another way to add or change entries.
- Diary and weight data are personal. Never commit it to a repository, including mfp-tool, which is public.
