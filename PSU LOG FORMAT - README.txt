========================================
PSU LOG FORMAT - README.TXT (SHORT)
========================================

COLUMNS
-------

timestamp_iso  : UTC timestamp when the sample/event was logged (ISO-8601).
t_rel_s        : Time in seconds since logging started.

CHx_P          : Power (W) of channel x, e.g. CH1_P, CH2_P.
                 (Other runs may also include CHx_V (voltage, V) and CHx_I (current, A)
                  depending on what was selected in the GUI.)

event          : Optional event name; empty for normal samples.
                 Examples:
                   CHn_SOFT_CROSS_UP    - Power crossed soft limit upwards.
                   CHn_SOFT_CROSS_DOWN  - Power went back below soft limit.
                   CHn_HARD_CROSS_UP    - Power crossed hard limit upwards.
                   CHn_HARD_CROSS_DOWN  - Power went back below hard limit.
                   CHn_HARD_TRIP        - Hard limit tripped; channel output cut.
                   CHn_LATCH_CLEARED    - Latch cleared after power dropped.

event_ch       : Channel related to the event (e.g. CH1, CH2).
event_v        : Voltage (V) at event time.
event_i        : Current (A) at event time.
event_p        : Power (W) at event time.


ROW TYPES
---------

1) Measurement row:
   - event columns empty.
   - Contains regular sampled values (CHx_P and, if enabled, CHx_V / CHx_I).

2) Event row:
   - event / event_ch / event_v / event_i / event_p filled.
   - Marks a limit crossing or trip at that instant.
