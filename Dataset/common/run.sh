#!/bin/sh -e
echo "RUN_ID: $RUN_ID" > /out/run.yaml
echo "RUN_NUM: $RUN_NUM" >> /out/run.yaml
echo "SCENARIO_ID: $SCENARIO_ID" >> /out/run.yaml
echo "SCENARIO_CONFIG: $SCENARIO_CONFIG" >> /out/run.yaml

cp /config/scenario.osc /out/scenario.osc
if [ -e /config/scenario.variant ]; then
    cp /config/scenario.variant /out/scenario.variant
fi
if [ -e /config/maps ]; then
    cp -r /config/maps /out/
fi

if [ -e /config/scenario.variant ]; then
    ros2 run scenario_execution_ros scenario_execution_ros -o /out /config/scenario.osc --post-run /config/common/post.sh --scenario-parameter-file /config/scenario.variant
else
    ros2 run scenario_execution_ros scenario_execution_ros -o /out /config/scenario.osc --post-run /config/common/post.sh
fi