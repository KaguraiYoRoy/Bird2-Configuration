#!/bin/bash
OLD_COMMIT=$(git rev-parse HEAD)

git pull

if [ $? -ne 0 ]; then
    echo "Failed to pull"
    exit 1
fi

NEW_COMMIT=$(git rev-parse HEAD)

if [ "$OLD_COMMIT" != "$NEW_COMMIT" ]; then
    birdc configure
    echo "$(date +'%Y-%m-%d %H:%M:%S'): $OLD_COMMIT->$NEW_COMMIT" >> log/update.log
fi