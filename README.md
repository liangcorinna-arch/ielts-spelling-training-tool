# IELTS Spelling Trainer

I was preparing my IELTS test and because I learn my English in a non-academy way, spelling turned out to be my weakness. I create this tool with the most mispelled words counted by IELTS in the json, it's not pretty but practical.

A command-line dictation drill. The computer says a word, you type it, it tells
you whether you were right and keeps score. Words you spell correctly three
times drop out of the deck, so what's left is always the stuff you still get
wrong.

## Files

| File | What it is |
|---|---|
| `spell trainer.py` | The app. Pure Python 3 |
| `words.json` | 325 IELTS words, each with an example sentence. |
| `progress.json` | Created on first run. Your scores live here. |
| `voices/` | Piper voice model (~60 MB), if installed. See below. |

Keep these in the same folder.

## Running it (Windows)

Open **PowerShell** or **Windows Terminal** in the folder and run:

```powershell
python "spell trainer.py"
```

The quotes matter because the filename has a space in it. That gives you a
20-word session. Speech uses Windows' built-in voice engine through PowerShell,
so there is nothing to install.

Run it from a real terminal rather than from inside Spyder, IDLE or a Jupyter
notebook. Those consoles handle typed input and colour inconsistently, and the
audio call can block them. If `python` isn't recognised, try `py -3` instead.

## During a session

Type the word you heard and press Enter. Or type one of these instead:

| Key | Does |
|---|---|
| *(nothing, just Enter)* | hear the word again |
| `r` | repeat the word |
| `s` | repeat it slowly |
| `h` | hint — first letter and number of letters |
| `c` | context — the example sentence with the word blanked out |
| `k` | skip (counts as a miss) |
| `q` | quit and save |

You can repeat/slow-repeat as many times as you like before answering — it
never counts against you.

Answers ignore capitals and stray spaces. When you get one wrong, the app shows
your spelling, a caret pointing at the first letter that diverged, and the
correct spelling — then asks you to **type the correct spelling yourself**
before moving on. Keep trying until you get it exactly right (you can still
`r`/`s`/Enter to hear it again while doing this). This doesn't change your
score — the miss was already recorded — it just makes sure the correct
spelling is the last thing your fingers typed, not just something you read.
Skipping with `k` is unaffected; that moves straight to the next word.

## How retiring works

Each word needs **3 correct answers** before it leaves the deck. Getting it
wrong resets that word's counter to zero — so three correct answers in a row,
not three lucky ones spread over a month. If you'd rather misses only count
against you without wiping the streak:

```powershell
python "spell trainer.py" --keep-streak
```

Miss counts are never reset by this. They accumulate for life so `--stats`
always shows your genuinely hardest words.

Word selection is weighted: words you've missed come round more often, and
words you've never seen outrank ones you're already close to retiring.

## Other commands

```powershell
python "spell trainer.py" -n 40              # longer session
python "spell trainer.py" --rate -3          # slower speech (-10 fast .. 10 slow)
python "spell trainer.py" --stats            # accuracy, recent sessions, worst words
python "spell trainer.py" --list             # every word and its 0/3 progress
python "spell trainer.py" --missed           # every word you've ever misspelled, full list
python "spell trainer.py" --export-missed my_weak_words.txt   # save that list to a file
python "spell trainer.py" --reset            # wipe progress, refill the deck
python "spell trainer.py" --no-color         # if your terminal shows escape codes
```

`--stats` only shows your worst 15 words. `--missed` shows every word that has
ever tripped you up, with a running total of how many times, for as long as
you keep the same `progress.json` — nothing is ever forgotten unless you
`--reset`. `--export-missed` writes that same list to a plain text file
(word + example sentence, one per line) so you can review it outside the app,
share it, or `--add-words` it back into a different deck.

## Adding your own words

Make a text file with one word per line. Optionally add an example sentence
after a `|`:

```
entrepreneur | She became an entrepreneur at twenty-three.
questionnaire
liaison | He acted as liaison between the two departments.
```

Then merge it in:

```powershell
python "spell trainer.py" --add-words my_words.txt
```

Duplicates and `#` comment lines are skipped. Your progress is untouched.

A good habit: whenever you misspell something in a practice test, add it to that
file and merge it. `--stats` will then tell you which of your own problem words
are actually improving.
