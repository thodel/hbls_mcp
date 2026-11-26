# HBLS gold standard

30 pages (`../../rebuild/gold_pages.txt`), transcribed by reading the page images only — not the
DigiBern text layer, not the database, not any OCR output. One JSON file per page,
`v{volume}_p{pdf_page}.json`. `pdf_page` is 1-based, as in the `#page=` of the DigiBern URLs.

Selection: KISTLER (IV 510), HALLENBARTER–HALLWIL (IV 68–72), ERLACH (III 68–69),
HÜNENBERG (IV 317–318), DIESBACH (II 731), the bibliographic abbreviations (I 11), a page of place
articles (I 14, AADORF/AARAU), and 17 pages drawn at random (seed 46) from the body of the eight
volumes (2 each, 3 from VIII), among pages with more than 6000 characters of text layer.

```json
{
  "volume": 4, "pdf_page": 510, "printed_page": 500,
  "running_head": "KISTENPASS KLARER",
  "articles": [
    {"headword": "KISSLING", "continued": true, "type": "bio",
     "end": "NZZ 1919, Nr. 1085. [H. Br.]"},
    {"headword": "KISTLER", "continued": false, "type": "fam",
     "start": "KISTLER. Familien der Kte. Bern und Schwyz.",
     "end": "der sog. KistlerAlp in Reichenburg. [R-r.]",
     "branches": [{"label": "A", "name": "Kanton Bern"}, {"label": "A.II", "name": "Familie K. in Aarberg"}],
     "members": [{"branch": "A", "nr": null, "name": "Peter", "birth": null, "death": null},
                 {"branch": "A.II", "nr": null, "name": "Hermann", "birth": 1853, "death": 1916}]},
    {"headword": "KLARER", "continued": false, "type": "fam", "start": "...", "end": null, "members": [...]}
  ],
  "cer_region": "c1b"
}
```

- `articles`: every article that has text on this page, in reading order (left column, then right).
  The first one is `continued: true` if the page starts in the middle of an article; its headword comes
  from the previous page. `headword` exactly as printed, in capitals, without the final full stop
  (`HALLER`, `HALLWIL, VON`, `DIESBACH, VON` — keep what the print shows, including commas and spaces).
- `type`: `fam` (family: a "Geschlecht"/"Familie" with members), `bio` (one person), `geo` (place, region,
  river, mountain, canton, monastery as a place), `tem` (subject, institution, event, term), `verweis`
  (only a cross-reference: "Siehe …").
- `start`: the first ~50 characters of the article as printed (for articles starting here); `end`: the last
  ~40 characters if the article ends on this page, else `null`.
- `branches`: the lettered/numbered parts of a family article ("A. Kanton Bern.", "I.", "II. Linie
  …"), as `label` (nested with a dot: `A.II`) and `name` (the words after the label, short).
- `members`: every person of a family article printed in small capitals (or letter-spaced bold) as a
  member — numbered ("— 3. DAVID, 1776-1850") or not ("— PETER, Mitglied der GC 1381"). `name` is the
  forename(s) as printed, in normal case ("Hans Jakob"); `nr` the printed number or null; `branch` the
  branch label it stands in or null; `birth`/`death` the years only (from "* 1853", "† 1916",
  "1832-1914"; `null` if not printed). Persons only named in passing in normal type are not members.
  Single-person (`bio`) articles have no members.
- `cer_region` and the transcription of that quarter: one quarter of the page (`c1a` = left column top half, `c1b` left bottom, `c2a`,
  `c2b`) transcribed completely as plain running text: no markup, no line breaks, words hyphenated at line
  ends joined, letter-spaced words written normally, small capitals in capitals, every symbol (†, *, ½)
  as printed. Lines cut by the image edge are left out; the halves overlap a little.

The transcriptions of the quarters are HBLS text (1921–1934, many authors). Until the rights for new full
text are settled they are not in this public repository: one file per page, `v4_p510.txt`, in
`$HBLS_GOLD_TEXT` (default `~/hbls_build/gold_text/`, on idhefix). `rebuild/evaluate.py` reads them from
there; without them it measures headwords and members only.
