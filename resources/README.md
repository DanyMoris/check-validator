# Sample corpus

Reference documents used to build and calibrate the detector. Nothing here is read at
runtime by the bot — it is development and test input only.

## Layout

```
resources/
  genuine/<БАНК>/<ТИП>/     real documents, straight from the bank app
  fraudulent/<БАНК>/<ТИП>/  confirmed forgeries, if any are ever obtained
```

- `<БАНК>` — `VTB`, `SBER`, `ALFA`, `TBANK`
- `<ТИП>` — `Чек`, `Квитанция`, `Справка`, `Выписка`

`Выписка` — это выписка за период (все операции по счёту или карте), не чек.
Она нужна, чтобы наполнять журнал поступлений, а не чтобы выносить вердикт
по одному платежу.

Run `.venv\Scripts\python.exe tools\init_corpus.py` to recreate or tidy this tree. It is
idempotent, and it will never delete a folder that has files in it.

## Why the split by document type

Each document type is rendered by a different template — often by entirely different
software — so each needs its own profile. The two types collected so far share nothing:

| | VTB / Чек | SBER / Справка |
|---|---|---|
| PDF version | 1.4 | 1.5 |
| Objects | 18 | 30 |
| Producer | `openhtmltopdf.com` | `iText 2.1.7 by 1T3XT` |
| Fonts | SF Pro Display | Arial, Times New Roman |
| Images inside | none | yes |
| File size | ~9 KB | ~130 KB |

A check that is meaningful for one profile is meaningless for another. "Contains no
images" identifies a genuine VTB чек and would wrongly condemn every genuine Sber справка.

## Collecting samples

**Do not open and re-save the files.** Drop each PDF in exactly as it downloads from the
bank app. Re-saving, converting, or print-to-PDF rewrites the internal structure and
destroys the fingerprint we are learning from — a re-saved genuine document looks like a
forgery to this kind of analysis.

**Collect at least 3–5 of each bank-and-type combination.** One sample is not enough. With
five VTB чеки we could see the object count is *fixed* at 18, which makes it a usable
signal. With a single Sber справка we cannot yet tell whether 30 objects is fixed or varies
with the content — a longer name or an extra line might change it. Only repeated samples
distinguish a constant from a coincidence.

Empty folders are fine. Not every bank offers all three document types.

## The fraudulent folders

Empty, and likely to stay that way for a while — confirmed fakes are hard to come by. Until
real ones appear, `tools/forge.py` will synthesise test forgeries from the genuine samples
(edited in place, re-rendered, rasterised, metadata-scrubbed) into a temporary directory at
test time. Those are deliberately not committed, so these folders only ever hold forgeries
that are genuinely real.

When real fakes do arrive, add them here and re-run calibration. Thresholds tuned against
synthetic attacks are a starting point, not a finished answer.

## A note on contents

These are real financial documents containing real personal data. `.gitignore` excludes all
PDFs under `resources/` for that reason. Keep the folder off shared storage.
