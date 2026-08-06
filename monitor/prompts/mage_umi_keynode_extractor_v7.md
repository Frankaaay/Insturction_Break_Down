This is one continuous UMI human-demonstration video, not a set of still images. A person carries UMI camera/gripper hardware while performing a basic manipulation. Dark devices that remain in the foreground and move with the hands are normally UMI apparatus, not phones, gloves, power tools, robot arms, or task objects.

The recording may be accepted or denied. The declared instruction says what was requested, not what actually happened. A denied recording may perform a different or opposite action. Do not use the instruction to fill in unseen motion.

Declared action: {declared_action}
Declared object: {declared_object}
Declared instruction: {declared_instruction}
Video duration: {video_duration_s} seconds

Analyze visible evidence in this order:
1. Determine the target object's state in the earliest clear frames without assuming the instruction happened.
2. Watch the full temporal motion and record only interactions or object changes that are actually visible. Repeated motion such as stirring must be supported across multiple different moments.
3. Determine the target object's state in the latest clear frames. Explicitly compare it with the initial state and state the direction of change.
4. Only after the visual account is complete, note whether it agrees with, contradicts, or is insufficient to evaluate the declared instruction. Do not output success/failure or monitor decisions.

Return ONLY compact JSON:

{{
  "video_duration_s": {video_duration_s},
  "apparatus_identification": "...",
  "initial_target_state": {{"object": "...", "state": "...", "evidence": "..."}},
  "visible_events": [
    {{"start_s": 0.0, "end_s": 0.0, "observation": "...", "object_change": "...", "evidence": "...", "uncertainty": "..."}}
  ],
  "final_target_state": {{"object": "...", "state": "...", "evidence": "..."}},
  "observed_change_direction": "...",
  "observed_action_summary": "...",
  "instruction_comparison": "agrees | contradicts | insufficient evidence",
  "unresolved": ["..."]
}}

Rules:
- All timestamps must be within 0.0 and {video_duration_s}. Use 0.0 only if the described event is visibly occurring in the first frame; otherwise give an approximate non-zero time.
- Do not invent standard phases, contact, lifting, release, reversal, or final state from the declared action.
- Camera or UMI apparatus motion alone is not an external-object change.
- If the declared object is unavailable or not visible, say unknown. Do not replace it with the UMI apparatus.
- Use at most 6 visible events. An empty list is valid.
- Output no markdown or text outside JSON.
