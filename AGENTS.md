# Repository remotes

- GitHub is the primary repository: `origin` points to `https://github.com/Frankaaay/planner_monitor.git`.
- JihuLab is the secondary repository: `jihulab` points to `git@jihulab.com:discoverrobotics/s2-restricted/agent/planner_monitor.git`.
- When pushing changes, push the current branch to both remotes, GitHub first and JihuLab second:

  ```powershell
  git push origin <branch>
  git push jihulab <branch>
  ```

- Only push to JihuLab after the GitHub push succeeds. Report the result of each push separately.
- A plain `git push` only updates the branch's configured upstream and must not be treated as synchronizing both repositories.
- Do not configure repository mirroring or combined multi-push URLs unless the user explicitly requests it.
