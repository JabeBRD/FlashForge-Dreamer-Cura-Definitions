# Cura Post Processing Script
# FlashforgeDreamer.py
#
# Cura 5.14
#
# FlashForge Dreamer:
#   T0 = RIGHT nozzle
#   T1 = LEFT nozzle
#
# Dreamer uses:
#   M108 T0
#   M108 T1
#
# This script:
#   1. Tracks active extruder from M108 T0/T1
#   2. Adds T0/T1 to M104 commands when missing
#   3. Removes M109 commands
#   4. Removes M105 commands
#   5. Removes standalone T0/T1 tool-change commands
#
# M108 T0/T1 commands are preserved.


from ..Script import Script
import re


class FlashforgeDreamer(Script):

    def getSettingDataString(self):
        return """{
            "name": "FlashForge Dreamer",
            "key": "FlashforgeDreamer",
            "metadata": {},
            "version": 2,
            "settings":
            {
                "enabled":
                {
                    "label": "Enable Dreamer G-code modification",
                    "description": "Adapts Cura G-code for FlashForge Dreamer.",
                    "type": "bool",
                    "default_value": true
                }
            }
        }"""

    def execute(self, data):

        if not self.getSettingValueByKey("enabled"):
            return data

        # Dreamer starts with T0 as the default assumption.
        active_extruder = 0

        for layer_index in range(len(data)):

            lines = data[layer_index].split("\n")
            output = []

            for line in lines:

                stripped = line.strip()

                # ---------------------------------------------------------
                # M108 T0 / M108 T1
                #
                # Track the active Dreamer extruder.
                # Keep the M108 line in the G-code.
                # ---------------------------------------------------------

                m108_match = re.match(
                    r"^\s*M108\s+T([01])(?:\s|;|$)",
                    stripped,
                    re.IGNORECASE
                )

                if m108_match:
                    active_extruder = int(m108_match.group(1))
                    output.append(line)
                    continue

                # ---------------------------------------------------------
                # Remove M105
                # ---------------------------------------------------------

                if re.match(
                    r"^\s*M105(?:\s|;|$)",
                    stripped,
                    re.IGNORECASE
                ):
                    continue

                # ---------------------------------------------------------
                # Remove M109
                # ---------------------------------------------------------

                if re.match(
                    r"^\s*M109(?:\s|;|$)",
                    stripped,
                    re.IGNORECASE
                ):
                    continue

                # ---------------------------------------------------------
                # Remove standalone T0 / T1 tool changes.
                #
                # Do NOT remove M108 T0 / M108 T1 here, because M108
                # was already handled above.
                # ---------------------------------------------------------

                if re.match(
                    r"^\s*T[01](?:\s*;.*)?$",
                    stripped,
                    re.IGNORECASE
                ):
                    continue

                # ---------------------------------------------------------
                # M104
                #
                # Add T0/T1 if the M104 does not already contain T0/T1.
                # ---------------------------------------------------------

                temp_match = re.match(
                    r"^(\s*)(M104)(\s+.*?)(\s*;.*)?$",
                    line,
                    re.IGNORECASE
                )

                if temp_match:

                    prefix = temp_match.group(1)
                    command = temp_match.group(2)
                    arguments = temp_match.group(3)
                    comment = temp_match.group(4) or ""

                    # Check whether M104 already has T0/T1.
                    has_tool = re.search(
                        r"(?:^|\s)T[01](?=\s|$)",
                        arguments,
                        re.IGNORECASE
                    )

                    if not has_tool:

                        arguments = arguments.rstrip()

                        line = (
                            prefix
                            + command
                            + arguments
                            + " T"
                            + str(active_extruder)
                            + comment
                        )

                output.append(line)

            data[layer_index] = "\n".join(output)

        return data