# OpenSolo MAVLink headers

The pinned `pymavlink` Yocto recipe generates the named `solo` dialect for both
Python and C from the owned Solo XML closure. C consumers include
`<opensolo/mavlink/solo/mavlink.h>` from the target sysroot. Build SoloLink through
the integration wrapper so its declared `pymavlink` dependency supplies headers.

The current upstream runtime and standard dialects are maintained separately
from the existing Solo firmware wire definitions. Message IDs, field layouts,
lengths and CRC extras are qualified against the retained protocol fixtures.
Do not regenerate a standard ArduPilot dialect over the Solo contract.
