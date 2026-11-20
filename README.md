# Resident Relocation Capacity

A Python starter project for a synthetic city-renewal data workflow. It contains input examples and their field contract only; no business analysis or decision result is implemented.

## Requirements

Python 3.12 or later. The starter uses only the standard library.

## Run the baseline checks

```powershell
python -m unittest discover -s tests -v
```

The baseline checks validate that the supplied CSV materials can be read and have rows. They do not verify the requested product behavior. See `docs/input-contract.md` for the source fields.
