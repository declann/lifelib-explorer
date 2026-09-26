# lifelib Explorer

[![Declan Naughton](lifelib_explorer/assets/dec.jpg)](https://calcwithdec.dev/about)

by [Declan Naughton](https://calcwithdec.dev/about)

An **experimental** explorer for [lifelib](https://lifelib.io)'s Python actuarial
models — interactive and visual.

lifelib is a collection of free and open-source actuarial models started by
**[Fumito Hamamura](https://www.linkedin.com/in/fumito-hamamura/)**, built on his [modelx](https://modelx.io) framework.

Here you can:

- Choose from a selection of models, or upload your own (many don't work yet,
  or aren't well suited)
- Interactively explore and manipulate model points, with cashflows
  recomputing as you drag
- Trace any cell's formula, value and precedents/dependents through modelx's
  graph, including the tables it reads
- Edit formulas and see the effects immediately, then download the modified
  model as a modelx zip (not an optimised workflow yet)

Fast feedback in modelling workflows — interpretation and communication as much as for development — is a keen interest of mine.
[Actuarial Playground](https://actuarialplayground.com) gets its fast,
interactive feedback from being based on calculang compiled to JavaScript\*.
lifelib Explorer shows that, thanks to Pyodide and WebAssembly, we can get a
similar experience from Python actuarial models: Python runs in the browser, with no
server or backend complexity.

It works on mobile — and there is also a PyQt desktop application.

**How it was made.** This was also an experiment in process: the code was
largely written by LLMs, driven from a terminal session in `tmux` — once on the
web, a good part of it from my phone. It is **largely vibe-coded**: I steered technical and design choices and
checked the behaviour, not much of the code. **Expect bugs**.

I continue to develop other modelling work and research through [calculang](https://calculang.dev). In 2025 I presented some aspects around this to the Society of Actuaries in Ireland, a presentation which is available [on YouTube](https://www.youtube.com/watch?v=3C1mojRzfBA). That presentation included Actuarial Modeller - on which some design elements here are based & still a WIP.




\* While calculang relies on JavaScript now, thanks to WebAssembly
future-calculang might not — and without sacrificing its core aims.


## Keyboard

| Shortcut | Action |
|---|---|
| `Ctrl+K` | focus the Inspector search |
| `↓` / `Enter` in search | move into results / open the selected cell |
| `Alt+←` / `Alt+→` | Inspector history back / forward |
| `Ctrl+R` | reset the point's fields |
| `Ctrl+Enter` / `Esc` in the formula editor | apply / cancel the edit |
