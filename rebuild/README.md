# HBLS rebuild from the scans

The database served until now was cut from the DigiBern text layer of the eight PDFs: letter-spaced
names ("P e t e r"), articles hidden inside others (HALLER inside HALLENBARTER), literature sigla
taken for headwords (SKL, SZGL …), articles cut at page breaks, and family articles without their
members (2,623 of 4,330). This folder rebuilds it from the page images.

| Step | Module | What |
|---|---|---|
| 0 | `gold/`, `evaluate.py` | 30 pages transcribed from the images; CER, headword F1, member F1 |
| 1 | `ocr.py` | page OCR, qwen3.8-27b on GPUStack, each column at 300 dpi, markup for headwords and members |
| 2 | `segment.py`, `abbrev.py`, `stoplist.txt` | cut into articles over columns and pages; sigla never open an article |
| 3 | `members.py`, `structure.py` | members by rule; branches, members, offices, kinship by qwen (JSON schema), checked by rules |
| 4–5 | `build.py` | the database: server schema plus stable ids, branches, offices, data version |
| — | `night_run.sh` | runs 1–5 by night (23–07) on idhefix, resumable |

Data and runs live on idhefix in `~/hbls_build/` (PDFs, `pages.db`, `structure.db`, `articles.jsonl`,
`hbls.staging.db`, logs). Nothing here touches the live database.

## Gold standard and measurements

30 pages: KISTLER (IV 510), HALLENBARTER–HALLWIL (IV 68–72), ERLACH (III 68–69), HÜNENBERG (IV 317–318),
DIESBACH (II 731), the bibliographic abbreviations (I 11), a page of place articles (I 14), and 17 pages
drawn at random. 148 articles start on them, 400 family members are printed on them (79 family
articles), and 59,303 characters are transcribed for the CER. See `gold/README.md`.

Measured 26.11.2026 (`python -m rebuild.evaluate`):

| | live database (text layer) | pilot: OCR + cut + qwen structure |
|---|---|---|
| CER, 29 pages without the 3-column abbreviation page | 6.69 % | **0.87 %** |
| CER, all 30 pages | 8.98 % | 1.95 % |
| Headword P / R / F1 | 0.86 / 0.60 / 0.71 | **0.98 / 0.98 / 0.98** |
| Member P / R / F1 (forename) | 0.71 / 0.25 / 0.37 | **0.92 / 0.93 / 0.93** |
| Member F1 with number | 0.37 | 0.92 |
| Matched members with both years right | 39 % | **97 %** |

Variants tried in the pilot (column OCR only, without cutting): whole page at 200 dpi, as ssrq_print does:
CER 1.19 %, headword F1 0.81; each column at 300 dpi: CER 0.96 %, headword F1 0.88 (prompt h2) and 0.98
after the prompt said that Æ/Œ in capitals are Ä/Ö (h4). Members by rule alone, without qwen: F1 0.80
(precision 0.95, recall 0.70); qwen lifts recall to 0.93 and the years from 58 % to 97 %.

The live figures are what the old cut delivers; the CER of the live database is measured on the text
layer it was cut from. Headwords are compared by lemma (the headword up to a comma or bracket), because
print, gold and OCR draw the end of "STEINMAUR (HERREN VON)" differently.

Known limits: the abbreviation pages are set in three columns and the OCR cuts them in two (CER 30 % on
I 11); the gold counts as members everyone printed in small capitals, including parents named in passing,
and takes bracketed years "(1421-1462)" as life dates where the print does not say otherwise.
