# Trajectories

`collect.py --run run-NN --share` writes:

```text
run-NN/
  README.md
  results.csv
  <problem>/trajectory.json
```

Trajectories are converted from saved agent records. Only graded, fully captured attempts are exported; the CSV accounts for other started attempts too. Original records stay private.

Keep repeated campaigns in separate run folders. Match records by campaign, problem ID and session ID. Review contents before publishing.
