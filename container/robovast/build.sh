#!/bin/bash -e
# Allow setting project from command line parameter
BASEDIR=$(dirname "$0")
ROS_DISTRO="jazzy"
PROJECT="localhost:5000"

# Parse command line arguments for project
while [[ $# -gt 0 ]]; do
  case $1 in
    --project)
      PROJECT="$2"
      shift 2
      ;;
    --ros-distro)
      ROS_DISTRO="$2"
      shift 2
      ;;
    --push|-n)
      PUSH=1
      shift
      ;;
    *)
      break
      ;;
  esac
done

# Pass remaining arguments to docker build
EXTRA_ARGS="$@"

echo "Using Dockerfile: $BASEDIR"
echo "From Context: $PWD"
echo "Project: $PROJECT"

# ensure PROJECT ends with a slash when non-empty
if [[ -n "${PROJECT}" ]]; then
  [[ "${PROJECT}" == */ ]] || PROJECT="${PROJECT}/"
fi
# build image
DOCKER_BUILDKIT=1 docker build \
  --build-arg ROS_DISTRO=$ROS_DISTRO \
  $EXTRA_ARGS \
  -t robovast_${ROS_DISTRO}:latest \
  -f $BASEDIR/Dockerfile \
  $PWD

docker tag robovast_${ROS_DISTRO} ${PROJECT}robovast_${ROS_DISTRO}

if [ -n "${PUSH:-}" ]; then
  echo "Pushing docker image to ${PROJECT}robovast_${ROS_DISTRO}"
  docker push "${PROJECT}robovast_${ROS_DISTRO}"
fi