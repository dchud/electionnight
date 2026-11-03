# Colophon

This page says how the monitor was built, what it runs on, where its data comes
from, and which published work it uses.
[Third-party notices](third_party_notices.md) has the licenses.

## How it was built

Claude Code, an AI coding agent from Anthropic, wrote the code, tests, and
documentation from the specification in
[`SPEC.md`](https://github.com/dchud/electionnight/blob/main/SPEC.md). A person
directed the work and decided what to merge.

## Tech stack

| Area | Tools | What they do here |
|---|---|---|
| Language and packaging | [Python](https://www.python.org/), [uv](https://docs.astral.sh/uv/) | The whole system runs on Python and installs with uv |
| Storage | [DuckLake](https://ducklake.select/), [DuckDB](https://duckdb.org/), [SQLite](https://sqlite.org/), [Apache Parquet](https://parquet.apache.org/) | DuckLake is the table format, with its catalog in SQLite and its data in Parquet. DuckDB reads and writes it |
| Backup | [Boto3](https://github.com/boto/boto3), [Amazon S3](https://aws.amazon.com/s3/) | Copies archives, catalog backups, and, on request, Parquet files to a private bucket |
| Collection | [HTTPX](https://www.python-httpx.org/), [feedparser](https://github.com/kurtmckee/feedparser), [Trafilatura](https://trafilatura.readthedocs.io/) | Fetch feeds, parse them, and extract article text |
| Local parsing | [Lingua](https://github.com/pemistahl/lingua-py), [FastEmbed](https://github.com/qdrant/fastembed), [ONNX Runtime](https://onnxruntime.ai/), [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small), [xxHash](https://github.com/ifduyue/python-xxhash), [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz), [NumPy](https://numpy.org/), [PyArrow](https://arrow.apache.org/docs/python/) | Detect language, embed text, hash titles, match names, do the vector math, and hand batches to DuckDB |
| Language models | [Anthropic Python SDK](https://github.com/anthropics/anthropic-sdk-python), [Claude](https://www.anthropic.com/claude) Haiku and Sonnet | Classification, extraction, translation, storyline labels and summaries, entity resolution, and briefs |
| Web | [FastAPI](https://fastapi.tiangolo.com/), [Uvicorn](https://github.com/encode/uvicorn), [sse-starlette](https://github.com/sysid/sse-starlette), [Jinja](https://jinja.palletsprojects.com/), [markdown-it-py](https://github.com/executablebooks/markdown-it-py), [htmx](https://htmx.org/) | Serve the dashboard and update it in the browser |
| Charts and maps | [Altair](https://altair-viz.github.io/), [Vega-Lite](https://vega.github.io/vega-lite/), [Vega](https://vega.github.io/vega/), [Vega-Embed](https://github.com/vega/vega-embed), [D3](https://d3js.org/), [TopoJSON Client](https://github.com/topojson/topojson-client), [US Atlas TopoJSON](https://github.com/topojson/us-atlas) | Draw the charts and maps |
| Type and style | [Tailwind CSS](https://tailwindcss.com/), [Inter](https://rsms.me/inter/), [Pygments](https://pygments.org/), [KaTeX](https://katex.org/) | Style the pages, set the text, and set the equations |
| Operations | [Typer](https://typer.tiangolo.com/), [Pydantic Settings](https://github.com/pydantic/pydantic-settings), [structlog](https://www.structlog.org/), [Honcho](https://github.com/nickstenning/honcho) | The command line, the settings, the logs, and running both processes |
| Testing and checks | [pytest](https://docs.pytest.org/), [Playwright](https://playwright.dev/python/), [Ruff](https://docs.astral.sh/ruff/), [mypy](https://mypy-lang.org/) | Tests, browser tests, formatting, linting, and types |
| Documentation | [MkDocs](https://www.mkdocs.org/), [Material for MkDocs](https://squidfunk.github.io/mkdocs-material/), [GitHub Pages](https://pages.github.com/) | Build and host this site |
| Project work | [Claude Code](https://www.anthropic.com/claude-code), [beads_rust](https://github.com/Dicklesworthstone/beads_rust), [GitHub Actions](https://github.com/features/actions) | Write the code, track the work, and run the checks |

## Data sources

| Data | Source | Where it lives | Terms |
|---|---|---|---|
| Outlet lean labels | [AllSides Media Bias Ratings](https://www.allsides.com/media-bias/media-bias-ratings), read from search results because allsides.com blocks direct fetches. The labels are abbreviated as L, LL, C, LR, and R. Pages show only the labels a person confirmed, and the file also holds unconfirmed ones | `config/lean_ratings.csv`, `reports/lean_review.md`, `/sources` | CC BY-NC 4.0 |
| Race ratings | Wikipedia's tables of ratings by Cook Political Report, Inside Elections, Sabato's Crystal Ball, Decision Desk HQ, Fox News, RealClearPolitics, Silver Bulletin, The Economist, FiftyPlusOne, Split Ticket, NBC News, and MS NOW | `config/races.yaml`, state pages | Each publisher keeps its rights |
| Races, candidates, and entities | [Wikipedia](https://en.wikipedia.org/), [Wikidata](https://www.wikidata.org/), and the [OpenFEC API](https://api.open.fec.gov/developers/). `config/entities_extra.yaml` lists the other pages behind its hand-added national figures | `config/races.yaml`, `config/entities_extra.yaml`, `ref` tables | Wikipedia text is CC BY-SA 4.0, Wikidata is CC0, and FEC data is a US government work |
| Poll closing times | [270toWin](https://www.270towin.com/poll-closing-times/), checked against [The Green Papers](https://www.thegreenpapers.com/G26/closing.phtml?format=gc) and Wikipedia | `config/poll_closings.yaml` | Facts, with sources named |
| Places | [US Census Bureau Gazetteer files](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html) | `ref` tables | US government work |
| Map shapes | [US Atlas TopoJSON](https://github.com/topojson/us-atlas), built from Census Bureau cartographic boundary files | `static/topo/` | ISC |
| News | Each outlet's feeds and articles. The outlets are listed on `/sources` | The local lake and archive files, and a private bucket when one is configured. Text goes to the Claude API for parsing. Article text and original pages are not in this repository, and the screens in the tour show a few headlines | Each outlet keeps its rights |

## Published work

### Storage

- Raasveldt, M., and Mühleisen, H.
  [The DuckLake Manifesto: SQL as a Lakehouse Format](https://ducklake.select/manifesto/).
- Raasveldt, M., and Mühleisen, H. (2019).
  [DuckDB: an Embeddable Analytical Database](https://doi.org/10.1145/3299869.3320212).
  *Proceedings of SIGMOD 2019*, 1981-1984.

### Text and clustering

- Wang, L., Yang, N., Huang, X., Yang, L., Majumder, R., and Wei, F. (2024).
  [Multilingual E5 Text Embeddings: A Technical Report](https://arxiv.org/abs/2402.05672).
  The embedding model.
- Charikar, M. S. (2002).
  [Similarity Estimation Techniques from Rounding Algorithms](https://doi.org/10.1145/509907.509965).
  *Proceedings of STOC 2002*, 380-388. SimHash.
- Manku, G. S., Jain, A., and Das Sarma, A. (2007).
  [Detecting Near-Duplicates for Web Crawling](https://doi.org/10.1145/1242572.1242592).
  *Proceedings of WWW 2007*, 141-150. The 64-bit hash at a distance of 3.
- Allan, J., Carbonell, J., Doddington, G., Yamron, J., and Yang, Y. (1998).
  [Topic Detection and Tracking Pilot Study: Final Report](https://ciir.cs.umass.edu/pubfiles/ir-137.pdf).
- Allan, J., Papka, R., and Lavrenko, V. (1998).
  [On-Line New Event Detection and Tracking](https://doi.org/10.1145/290941.290954).
  *Proceedings of SIGIR 1998*, 37-45.
- Yang, Y., Pierce, T., and Carbonell, J. (1998).
  [A Study of Retrospective and On-Line Event Detection](https://doi.org/10.1145/290941.290953).
  *Proceedings of SIGIR 1998*, 28-36.
- Grootendorst, M. (2022).
  [BERTopic: Neural topic modeling with a class-based TF-IDF procedure](https://arxiv.org/abs/2203.05794).
  The storyline keyword score is adapted from c-TF-IDF.
- Barbaresi, A. (2021).
  [Trafilatura: A Web Scraping Library and Command-Line Tool for Text Discovery and Extraction](https://doi.org/10.18653/v1/2021.acl-demo.15).
  *Proceedings of ACL-IJCNLP 2021: System Demonstrations*, 122-131.

### Statistics

- Abramowitz, M., and Stegun, I. A., eds. (1964).
  [*Handbook of Mathematical Functions*](https://personal.math.ubc.ca/~cbm/aands/page_949.htm),
  eq. 26.7.5. National Bureau of Standards. The $t$ quantile in the tone
  intervals.

### Charts and maps

- Satyanarayan, A., Moritz, D., Wongsuphasawat, K., and Heer, J. (2017).
  [Vega-Lite: A Grammar of Interactive Graphics](https://doi.org/10.1109/tvcg.2016.2599030).
  *IEEE TVCG* 23(1), 341-350.
- Satyanarayan, A., Russell, R., Hoffswell, J., and Heer, J. (2016).
  [Reactive Vega: A Streaming Dataflow Architecture for Declarative Interactive Visualization](https://doi.org/10.1109/tvcg.2015.2467091).
  *IEEE TVCG* 22(1), 659-668.
- VanderPlas, J., Granger, B., Heer, J., Moritz, D., Wongsuphasawat, K.,
  Satyanarayan, A., Lees, E., Timofeev, I., Welsh, B., and Sievert, S. (2018).
  [Altair: Interactive Statistical Visualizations for Python](https://doi.org/10.21105/joss.01057).
  *Journal of Open Source Software* 3(32), 1057.
- Bostock, M., Ogievetsky, V., and Heer, J. (2011).
  [D³: Data-Driven Documents](https://doi.org/10.1109/tvcg.2011.185).
  *IEEE TVCG* 17(12), 2301-2309.
- Havre, S., Hetzler, B., and Nowell, L. (2000).
  [ThemeRiver: Visualizing Theme Changes over Time](https://doi.org/10.1109/infvis.2000.885098).
  *Proceedings of IEEE InfoVis 2000*, 115-123. The Storyline Stream's form.
- Byron, L., and Wattenberg, M. (2008).
  [Stacked Graphs: Geometry & Aesthetics](https://doi.org/10.1109/tvcg.2008.166).
  *IEEE TVCG* 14(6), 1245-1252. The Storyline Stream's form.
- Tufte, E. R. (2006).
  [*Beautiful Evidence*](https://www.edwardtufte.com/notebook/sparkline-theory-and-practice-edward-tufte/),
  pp. 46-63. Sparklines.
- Harrower, M., and Brewer, C. A. (2003).
  [ColorBrewer.org: An Online Tool for Selecting Colour Schemes for Maps](https://doi.org/10.1179/000870403235002042).
  *The Cartographic Journal* 40(1), 27-37. The tone scale.

## Related work

These came before this project and do related things. They are listed for
comparison. This repository records no use of them.

- [Ground News](https://ground.news/about) compares a story's coverage by
  outlet lean.
- Wang, J. S., Haider, S., Tohidi, A., Gupta, A., Zhang, Y., Callison-Burch,
  C., Rothschild, D., and Watts, D. J. (2025).
  [Media Bias Detector: Designing and Implementing a Tool for Real-Time Selection and Framing Bias Analysis in News Coverage](https://arxiv.org/abs/2502.06009).
  It labels topic, tone, and lean with language models.
- Steinberger, R., Pouliquen, B., and van der Goot, E. (2009).
  [An introduction to the Europe Media Monitor family of applications](https://arxiv.org/abs/1309.5290).
  It clusters news from feeds and detects breaking stories.
- Roberts, H., and others (2021).
  [Media Cloud: Massive Open Source Collection of Global News on the Open Web](https://ojs.aaai.org/index.php/ICWSM/article/view/18127).
  *Proceedings of ICWSM* 15(1), 1034-1045. It measures attention across
  outlets.
- Pew Research Center's Project for Excellence in Journalism (2008).
  [Campaign Coverage Index](https://www.pewresearch.org/journalism/dataset/2008-campaign-coverage-index/).
  It measured the campaign's share of news coverage.
- [Meta Newsroom](https://www.cjr.org/united_states_project/meta_newsroom_liveblogging_the_2012_election.php),
  Columbia Journalism Review, 2012. Students reported live on the election
  night coverage.
- [World Monitor](https://github.com/koala73/worldmonitor) is an open-source
  dashboard of feeds, a map, and model-written briefs.
