This is a continuous UMI human-demonstration video. Dark devices moving with the operator's hands are UMI camera/gripper apparatus, not task objects.

The declared instruction is an untrusted collection label. This may be a denied recording that performs the opposite action. Do not repeat the label as the observation.

Declared instruction: {declared_instruction}
Target object: {declared_object}
Video duration: {video_duration_s} seconds

For this object, explicitly compare these competing visible hypotheses:
{candidate_outcomes}

Inspect the earliest clear frames and latest clear frames first. Then inspect the motion between them and select only the hypothesis supported by visible evidence.

Return ONLY JSON:
{{
  "initial_state": {{"state": "...", "evidence": "..."}},
  "events": [{{"start_s": 0.0, "end_s": 0.0, "observation": "...", "evidence": "..."}}],
  "final_state": {{"state": "...", "evidence": "..."}},
  "observed_transition": "...",
  "selected_hypothesis": "...",
  "rejected_hypotheses": [{{"hypothesis": "...", "reason": "..."}}],
  "uncertainty": ["..."]
}}

All timestamps must be between 0.0 and {video_duration_s}. Do not infer success/failure. Do not invent contact, motion, or reversal from the instruction.
