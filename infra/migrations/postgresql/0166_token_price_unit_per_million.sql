UPDATE models
SET input_price_per_1k = input_price_per_1k * 1000,
    output_price_per_1k = output_price_per_1k * 1000;
UPDATE model_price_plans
SET input_price_per_1k = input_price_per_1k * 1000,
    output_price_per_1k = output_price_per_1k * 1000;
UPDATE price_snapshots
SET input_price_per_1k = input_price_per_1k * 1000,
    output_price_per_1k = output_price_per_1k * 1000;
UPDATE workspace_provider_cost_rate_versions
SET input_rate_micros_per_1k = input_rate_micros_per_1k * 1000,
    output_rate_micros_per_1k = output_rate_micros_per_1k * 1000;
UPDATE platform_provider_reference_rate_releases
SET input_rate_micros_per_1k = input_rate_micros_per_1k * 1000,
    output_rate_micros_per_1k = output_rate_micros_per_1k * 1000;
