You are analyzing a continuous video extracted from a UMI (Universal Manipulation Interface) human-demonstration recording.

UMI recording context:
- A human operator carries and controls UMI data-collection/gripper hardware to demonstrate one basic manipulation task, such as picking up, carrying, stirring, opening, or closing an object.
- Dark or black devices that stay in the foreground and move with the operator's hands are normally the UMI manipulation/data-collection apparatus. Do not describe them as phones, gloves, power tools, robot arms, or task objects unless the video provides decisive contrary evidence.
- The viewpoint is attached to the UMI apparatus. Camera motion and apparatus motion are expected. They are not object-state changes by themselves.
- This is a continuous video, not an unordered set of still images. Compare earlier and later frames and use motion over time, especially for repeated actions such as stirring.

Declared collection instruction:
- Action: {declared_action}
- Object: {declared_object}
- Full instruction: {declared_instruction}
- Instruction source: {instruction_source}

The declared instruction is context for locating the likely interaction; it is not proof that the action happened or succeeded. Report what is visibly observed even when it contradicts the instruction. If the object is not provided, keep it unknown until the video itself supports an identity.

This video ends at exactly {video_duration_s} seconds. Analyze the complete video chronologically and return ONLY one compact JSON object:

{{
  "video_duration_s": {video_duration_s},
  "umi_apparatus": {{
    "identified": "yes | no | uncertain",
    "evidence": "..."
  }},
  "declared_task": {{
    "action": "...",
    "object": "...",
    "source": "..."
  }},
  "initial_state": [
    {{"object": "...", "state": "...", "evidence": "..."}}
  ],
  "key_nodes": [
    {{
      "start_s": 0.0,
      "end_s": 0.0,
      "phase": "approach | contact | manipulate | release | retreat | other",
      "manipulated_object": "...",
      "before": "...",
      "visible_interaction": "...",
      "after": "...",
      "motion_evidence": "...",
      "uncertainty": "..."
    }}
  ],
  "observed_action_summary": "...",
  "final_state": [
    {{"object": "...", "state": "...", "evidence": "..."}}
  ],
  "instruction_observation_conflicts": ["..."],
  "unresolved": ["..."]
}}

Requirements:
- Every timestamp must satisfy 0.0 <= timestamp <= {video_duration_s}. There is no content after {video_duration_s} seconds.
- Report at most 8 key nodes. Use multiple chronological nodes when approach, contact, manipulation, release, or retreat are visibly separable.
- For repeated manipulation such as stirring, describe the motion trajectory across multiple frames; do not call it static merely because the container stays in place.
- Treat foreground UMI hardware as apparatus, not as the manipulated object. Focus on the external object contacted or affected by that apparatus.
- Robot/apparatus/camera motion alone is not a task-object state change.
- Do not infer success or failure from the instruction. Do not invent a reversal or final state that is not visible.
- Use "unknown" for occluded identity, contact, or state. An empty key_nodes list is allowed when no interaction is verified.
- Do not use default-length video segments. Do not output markdown or text outside JSON.
