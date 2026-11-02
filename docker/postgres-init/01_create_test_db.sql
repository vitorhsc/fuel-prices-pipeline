-- Separate database for the integration tests, so `make test` never touches real data.
CREATE DATABASE pipeline_test OWNER pipeline;
