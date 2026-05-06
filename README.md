# Grida Standard Library

Grida Standard Library is a 100% free & open-source handpicked collection of design assets used by [Grida](https://github.com/gridaco/grida)'s editor and AI.

Browse the live library at [grida.co/library](https://grida.co/library).

Categories:

- fonts
- shapes
- symbols
- logos
- patterns
- wallpapers
- icons
- colors
- sizes
- objects
- components
- styles
- illustrations
- 3d-illustrations
- doodles
- scribbles

## (Working Draft) — API coming soon

## The Asset Object Model

```json
{
  "object": "o",
  "id": "o_x1234",
  "name": "logo-grida.svg",
  "description": "Grida logo svg with black path fill",
  "type": "application/svg+xml",
  "keywords": ["logo", "grida"],
  "category": "logo",
  "categories": ["logos"],
  "author": {
    "name": "Grida",
    "url": "https://grida.co/grida"
  },
  "version": 1,
  "width": 64,
  "height": 64,
  "bytes": 1234,
  "url": "https://grida.co/library/v1/o_x1234",
  "urls": {
    "raw": "https://grida.co/library/v1/o_x1234"
  },
  "transparency": true,
  "visual_padding": [2, 2, 2, 2],
  "color": "#000000",
  "colors": ["#000000"],
  "background": "#FFFFFF",
  "svg": "<svg>...</svg>",
  "score": 0.5,
  "year": 2025,
  "created_at": 1677610602,
  "updated_at": 1677610602
}
```

## Metadata

- generator — the generator used to create the asset
  - grida-canvas
  - the-bundle
  - dall-e-3
  - midjourney
  - photoshop

- entropy — the visual complexity of the design
  - 0 ~ 1

## Scripts

**Setup**

```sh
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

**optimize.py**: Optimize images.

```sh
# optimize
python optimize.py optimize ~/Public/library/category  ~/Public/library/category/out --max-size=3

# rmsmall — remove small images
python optimize.py rmsmall ~/Public/library/category
```

**metadata.py**: Generate metadata for images.

```sh
python metadata.py /path/to/process --type=jpg
```

outputs `.metadata.json` files.

**describe.py**: Describe images using ollama.

Needs ollama installed. Needs model with vision support.

```sh
python describe.py /path/to/process --model=gemma3:27b
```

outputs `.describe.json` files.

**object.py**: Combines all metadata for library uploads.

```sh
python object.py /path/to/process
```

outputs `.object.json` files.

**upload.py**: Upserts the object (file) and metadata (row) to the Grida Library.

```sh
python upload.py /path/to/process
```

## See Also

Other open Grida asset libraries:

- [fonts.grida.co](https://fonts.grida.co) — Google fonts indexed with SVGs & metadata ([gridaco/fonts](https://github.com/gridaco/fonts))
- [icons.grida.co](https://icons.grida.co) — Grida Icons Library, hand-picked and meta-described ([gridaco/icons](https://github.com/gridaco/icons))
- [The Bundle](https://grida.co/bundle) — A collection of 3D-rendered illustrations

And the main project:

- [Grida](https://github.com/gridaco/grida) — open-source canvas editor & rendering engine

## License

Released under [CC0 1.0 Universal](./LICENSE) — public domain.
