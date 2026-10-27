# Fixtures

## `lhm_data_sample.json` is SYNTHETIC (for now)

This file was **not** captured from the real laptop. Phase 1 was built in a
cloud container that could not reach `http://127.0.0.1:8085/data.json` on
the Windows machine. The file was therefore written by hand to follow the
structure described for LibreHardwareMonitor 0.9.6 (`Text`, `HardwareId`,
`Children`, `SensorId`, `Type`, `Value`, `RawValue`). The values (4.5 W idle
package, 39.7 W max, dead Memory/Platform sensors) come from the readings
taken manually in the GUI.

The computer node is named `SYNTHETIC-SAMPLE-replace-with-real-data-json` so
this cannot be mistaken for a real capture.

### Replacing it with the real capture

In PowerShell, with LibreHardwareMonitor's Remote Web Server running:

```powershell
Invoke-WebRequest http://127.0.0.1:8085/data.json -OutFile fixtures\lhm_data_sample.json
python -m greensettle.cli dump-powers --file fixtures\lhm_data_sample.json
python -m pytest
```

If the parser tests fail on the real file, the real structure differs from
what was assumed. Compare the printed subtree with
`greensettle/agent/lhm_client.py` (`find_cpu_node`, `find_powers_group`,
`parse_watts`).
