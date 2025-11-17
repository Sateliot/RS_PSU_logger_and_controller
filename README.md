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

<<<<<<< Updated upstream
# NOTES
=======
# GUI NOTES
>>>>>>> Stashed changes
-----
- **A CSV log is created only while "Start Plot" is active**.
- Limits are enforced in the background by the worker process.
- The GUI remains responsive during logging and plotting.

# DEPENDENCIES
========================================

Python Version
--------------
Python 3.10 or newer recommended.


Python Libraries
----------------
Install required packages:

    pip install rsinstrument
    pip install matplotlib
    pip install pyserial

(These are the only external libraries needed.)


System Requirements
-------------------

Rohde & Schwarz VISA (mandatory)
--------------------------------
You must install **R&S VISA** so the application can communicate
with the NGE power supply via USB.

Windows installer:
    RS_VISA_Setup_Win_7_2_6.exe  
or from R&S website:
    https://www.rohde-schwarz.com/fi/applications/r-s-visa-application-note_56280-148812.html

Linux (example .deb):
    https://scdn.rohde-schwarz.com/ur/pws/dl_downloads/dl_application/application_notes/1dc02___rs_v/rsvisa_5.12.9_amd64.deb

After installation, ensure "rsvisa" backend is selected in RsInstrument.


Optional
--------
None. Application runs with the above dependencies only.
