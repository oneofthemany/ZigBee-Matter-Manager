import yaml
from pathlib import Path

def load_yaml_config(filepath: Path) -> dict:
    """
    Loads configuration data from a YAML file.

    Args:
        filepath (Path): The path object pointing to the YAML configuration file.

    Returns:
        dict: The loaded configuration as a dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
        yaml.YAMLError: If there is an issue parsing the YAML content.
    """
    if not filepath.exists():
        raise FileNotFoundError(f"Configuration file not found at: {filepath}")

    try:
        with open(filepath, 'r') as f:
            config_data = yaml.safe_load(f)
        return config_data if config_data is not None else {}
    except yaml.YAMLError as e:
        print(f"Error parsing YAML file: {e}")
        raise
    except IOError as e:
        print(f"Error reading config file: {e}")
        raise

# Example usage (will not run when imported):
if __name__ == "__main__":
    try:
        # Assuming a config.yaml exists in the parent directory for a test run
        test_path = Path(__file__).parent.parent / "config.yaml"
        test_config = load_yaml_config(test_path)
        print("Successfully loaded configuration structure:")
        print(test_config)
    except Exception as e:
        print(f"Test failed: {e}")