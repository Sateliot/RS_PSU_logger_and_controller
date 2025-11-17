# HOW TO RUN
----------
From a terminal or PowerShell:

```
    python rs_psu_gui.py
```

Make sure the worker file (rs_psu_worker.py) is in the same folder.


# BUTTONS & CONTROLS
------------------

## Connect / Disconnect
pens or closes the VISA connection to the power supply.

## Channel Controls (CH1 / CH2 / CH3)
Vset / Iset fields: Set voltage (V) and current (A) limits.
ON/OFF button: Enables or disables that output channel.
Hard/Soft limit fields (optional): Used by the watchdog to trip or warn.

## Main Output ON/OFF
Toggles the PSU global output state.

## Live Plot & Log
V / I / P checkboxes: Select which values to plot for each channel.
Sampling (ms): Sets the plot/log update interval.
Start Plot / Stop Plot:
- Starts live graphing.
- Prompts for a CSV log filename.
- Stops plotting/logging when pressed again.

## Status Bar
Shows connection state, worker status, and any errors.

![Screenshot of the GUI.](https://github.com/Sateliot/RS_PSU_logger_and_controller/blob/main/GUI_screenshot.png)

# NOTES
-----
- A CSV log is created only while "Start Plot" is active.
- Limits are enforced in the background by the worker process.
- The GUI remains responsive during logging and plotting.
