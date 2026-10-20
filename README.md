# Raspberry Pi 5 board reconstruction

A KiCad 10 reconstruction of the Raspberry Pi 5 board (part placement, part identification, nets traced from the
real board), published as an interactive boardview for repair work.

* **View it:** <https://dpetzel.github.io/pi5-boardview/>, rebuilt from `main` on every push
* **Download:** `pi5-boardview.html` (self-contained, works offline) and `pi5.brd` (for
  [OpenBoardView](https://github.com/OpenBoardView/OpenBoardView)) from the [releases](../../releases)

## Repository

| Path | What |
|---|---|
| `pi5.kicad_pcb`, `pi5.kicad_pro` | The board. Top and bottom photos are embedded as reference images (Eco1.User = top, Eco2.User = bottom) |
| `pi5.pretty/`, `DA9091.pretty/` | Project footprints |
| `pi5_board_top.png`, `pi5_board_bottom.png` | The source photos embedded in the board |
| `tools/boardview.py`, `tools/template.html` | Exporter (HTML viewer + `.brd`) and the viewer it fills in |
| `.github/workflows/boardview.yml` | CI: builds the boardview, deploys Pages, attaches files to releases |

The boardview files are build output and not committed.

## Contributing

Edit the board in KiCad 10 and open a pull request. Part details go in footprint fields: `Function`, `Part`, `Notes`,
`Replacement`, and `Source` (`Source2`, `Source3`, … for more links). Fields holding a URL show as links in the
viewer. References ending in `**` or `?` (`REF**`, `R?`) are shown as unidentified parts.

Every pull request builds the boardview. Download the `boardview` artifact from the PR's workflow run to check
your change before it's merged.

### Building locally

The exporter needs KiCad's `pcbnew` Python module. With Docker:

```sh
docker run --rm -v "$PWD":/w -w /w kicad/kicad:10.0 python3 tools/boardview.py pi5.kicad_pcb
```

or with the Python bundled with KiCad (on Windows, `"C:\Program Files\KiCad\10.0\bin\python.exe"`):

```sh
python3 tools/boardview.py pi5.kicad_pcb
```

Both write `boardview/pi5-boardview.html` and `boardview/pi5.brd`. Options: `-o DIR`, `--no-photo`,
`--kicad-nets-only` (skip nets derived from copper; see the notes at the top of `tools/boardview.py`).
KiCad exports the last *saved* board, so save before building.

## Releases

Push a tag such as `v1.0` and CI attaches that build's `pi5-boardview.html` and `pi5.brd` to a GitHub Release.

## License

[CC-BY-SA-4.0](LICENSE): share and adapt with attribution, and publish adaptations under the same license.
