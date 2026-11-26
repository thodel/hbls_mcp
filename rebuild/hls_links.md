# HBLS ↔ HLS: Verknüpfung und Stichprobe

Schritt 4 des Neubauplans. Code: `rebuild/hls_links.py`, Tests: `test_hls_links.py`. Stand 26.11.2026.

## Lauf

```bash
HLS_MCP_AUTH='Basic …' python -m rebuild.hls_links --db hbls.db --out hls_links.db --cache hls_cache.db
```

- Eingabe: Kopie der Live-DB `idhefix:/home/tobias/mcp/data/hbls.db` (nur `articles`, `members`; 18'244 Artikel,
  19'707 Mitglieder). Die Live-DB wurde nur gelesen.
- HLS über `https://tei.dh.unibe.ch/mcp/hls/mcp`, ein Aufruf pro 0,5 s, alle Antworten im Cache:
  9'006 Aufrufe beim ersten Lauf (rund 80 Minuten), danach 26. Ein erneuter Lauf braucht das Netz kaum noch.
- Ausgabe: `article_links` mit 3'658 Verknüpfungen und `article_link_review` mit 228 Fällen, die von Hand
  entschieden werden müssen.

| Art | Artikel/Mitglieder | sicher | wahrscheinlich | möglich | zur Prüfung |
|---|---:|---:|---:|---:|---:|
| Familie (`fam`) | 7'489 | 774 | 322 | 75 | 131 |
| Ort (`geo`) | 3'201 | 998 | 131 | 59 | 96 |
| Person, eigener Artikel (`bio`) | 2'029 | 80 | 135 | 26 | – |
| Familienmitglied (`member`) | 19'707 | 419 | 413 | 226 | 1 |

Die Zahlen zählen verschiedene `hbls_id`. Die heutige DB enthält viele Artikel doppelt, deshalb ist die Zahl der
Zeilen höher. Nicht verknüpft wurden 3'615 Artikel ohne erkennbaren Typ (Sachartikel, Fortsetzungen) und 1'910
Verweise («Siehe …») und Literaturkürzel.

## Warum es nicht mehr Verknüpfungen sind

- **Das HLS-MCP ist kleiner als das HLS.** Es hat nur 2'572 Familien- und 3'867 Ortsartikel. Die meisten HBLS-Familien
  und viele Gemeinden haben dort keinen Titel.
- **Die heutige DB hat keine Typen.** Familie, Ort oder Person werden aus dem Artikelanfang geraten. Der Neubau
  (Schritt 2/3) bringt Typen und strukturierte Mitglieder; dann sollte `run()` diese statt der Heuristik nutzen.
- **Mitglieder:** Nur 12'439 der 19'707 Mitglieder haben ein Lebensjahr. Ohne Jahr wird nicht verknüpft.
  Zudem hat das HLS zu den meisten HBLS-Mitgliedern keinen Personenartikel.

## Stichprobe: 50 Fälle von Hand

Gezogen geschichtet nach Art und Stufe (Seed 20261126) aus dem ersten Lauf. Jede Verknüpfung wurde am HBLS-Text
und am HLS-Artikel (Titel, Kanton, Lebensdaten, Lead) geprüft.

| Stufe | Lauf 1 richtig | Lauf 2 (nach den Korrekturen) richtig |
|---|---:|---:|
| sicher | 23 / 23 | 24 / 24 |
| wahrscheinlich | 13 / 16 | 12 / 13 |
| möglich | 6 / 11 | 5 / 7 |
| **gesamt** | **42 / 50 (84 %)** | **41 / 44 (93 %)**, 6 Fälle neu zur Prüfung bzw. ohne Link |

Nach Art (Lauf 2): Familie 14/16, Ort 10/11, Mitglied 14/14, Person 6/6.

**Fehler in Lauf 1 und was daraus wurde:**

| Nr. | HBLS | HLS | Fehler | Lauf 2 |
|---|---|---|---|---|
| 9 | ZEHNDER (Einsiedeln) | Zehnder (Gränichen AG) | andere Familie | zur Prüfung (Kanton SZ ≠ AG) |
| 11 | WOLHUSEN (Dienstmannen in Solothurn) | von Wolhusen (Freiherren LU) | andere Familie gleichen Namens | bleibt falsch (wahrscheinlich) |
| 15 | RAUCH (GR) | Rauch (Diessenhofen TG) | andere Familie | zur Prüfung |
| 16 | MASSMÜNSTER (Münchenstein, 18. Jh.) | von Massmünster (Elsässer Adel) | andere Familie | bleibt falsch (möglich, Zeit passt nicht) |
| 17 | GRÜNENFELS (Liestal) | von Grünenfels (Rätischer Adel) | andere Familie | zur Prüfung |
| 27 | FELDBACH (Weiler ZH) | Feldbach (Kloster TG) | Ort ≠ Kloster | kein Link (Kloster über `lexical_class` erkannt) |
| 29 | PIGNIEU (Schams) | Pigniu (Panix, Surselva) | ähnlicher Name, anderer Ort | bleibt falsch (möglich) |
| 30 | ROCHE (La Roche FR) | Roche (VD) | anderer Ort | zur Prüfung |

Ein richtiger Fall fällt in Lauf 2 heraus: TEUFEN (Familien von Schaffhausen und Zürich) ↔ von Teufen (ZH) ist
jetzt zur Prüfung, weil im Artikelkopf nur Schaffhausen erkannt wurde.

**Korrekturen nach der Stichprobe:**
- Gleicher Name, aber anderer Kanton wird nicht mehr als «möglich» verknüpft, sondern geht in `article_link_review`.
- HLS-Ortstitel ohne Zusatz («Feldbach») erhalten den Zusatz aus `lexical_class` (Kloster, Burg, Herrschaft).
- Kantonsnamen werden auch erkannt, wenn ein Wort direkt folgt («Stadt Bern seit»). Ein Ort am Artikelanfang
  zählt ebenfalls («von Einsiedeln.»).
- Mitglieder-IDs: Die Nummern beginnen in jedem Zweig neu und fehlen manchmal. Die ID ist jetzt `#3`, wenn die
  Nummer im Artikel eindeutig ist, sonst `#3.2`, und `#x1` ohne Nummer (`member_keys`).

## Lesart der Stufen

- **sicher**: in der Stichprobe ohne Fehler. Kann ohne Durchsicht übernommen werden.
- **wahrscheinlich**: etwa 1 Fehler auf 13. Die Fehler sind gleichnamige Familien, bei denen der HBLS-Artikel
  keinen Kanton nennt.
- **möglich**: etwa jeder dritte Fall ist falsch. Nur mit Durchsicht übernehmen; ähnliche Schreibung ist bei Orten
  besonders unsicher.
- `article_link_review`: 228 Fälle mit mehreren gleich guten Kandidaten (`mehrdeutig`) oder gleichem Namen in
  einem anderen Kanton (`prüfen`).

## Für den Neubau

- `article_links` ist bewusst eine eigene Tabelle mit stabilen IDs (`hbls-{band}-{seite}-{STICHWORT}[#{nr}]`).
  Ändern sich Band, Seite oder Stichwort im Neubau, muss die Tabelle neu gebaut werden. Das geht dank Cache schnell.
- Seitenzahl ist heute die PDF-Seite. Wenn der Neubau die gedruckte Seite einführt, ändern sich die IDs.
- Wikidata ist noch nicht verknüpft. Der nächste Schritt wäre: HLS-ID → Wikidata über P902 (HLS-ID) per SPARQL,
  ein Aufruf für alle.
