#!/usr/bin/env python3
"""
Configuration loader for Test Results Analyzer
Handles loading and saving settings from/to configuration files
"""

import configparser
from pathlib import Path
from typing import Any, Dict, Optional


class ConfigLoader:
    """Configuration loader that handles reading and writing configuration files"""

    def __init__(self, config_file: Optional[str] = None):
        """
        Initialize the configuration loader

        Args:
            config_file: Path to the configuration file. If None, uses default path.
        """
        if config_file is None:
            # Default to result_analyzer.cfg in the project root
            self.config_file = Path(__file__).parent.parent.parent.parent / "result_analyzer.cfg"
        else:
            self.config_file = Path(config_file)

        self.config = configparser.ConfigParser()
        self._defaults = self._get_default_config()
        self.load_config()

    def _get_default_config(self) -> Dict[str, Dict[str, Any]]:
        """Get the default configuration values"""
        return {
            'general': {
                'auto_expand_tree': True,
                'remember_window_state': True,
                'auto_play_video': False,
                'max_file_size_mb': 50
            },
            'ui': {
                'theme': 'System Default',
                'font_size': 9,
                'video_controls_always_visible': False,
                'video_volume': 50
            },
            'advanced': {
                'max_tree_depth': 3,
                'enable_thumbnails': True,
                'log_level': 'INFO',
                'enable_debug_output': False
            },
            'notebooks': {
                'analysis_single_test': 'analysis/analysis_single_test.ipynb',
                'analysis_single_variant': 'analysis/analysis_single_variant.ipynb',
                'analysis_run': 'analysis/analysis_run.ipynb',
                'overview_single_test': 'analysis/overview_single_test.ipynb',
                'overview_single_variant': 'analysis/overview_single_variant.ipynb',
                'overview_run': 'analysis/overview_run.ipynb'
            },
            'execution': {
                'rosbag_2_csv_conversion_command': ''
            },
            'directories': {
                'results_dir': '../../../downloaded_files'
            }
        }

    def load_config(self) -> None:
        """Load configuration from file"""
        if self.config_file.exists():
            try:
                self.config.read(self.config_file)
                print(f"Loaded configuration from {self.config_file}")

                # Validate and ensure all required sections exist
                self._validate_and_fix_config()

            except Exception as e:
                print(f"Error loading config file {self.config_file}: {e}")
                print("Creating default configuration...")
                self._create_default_config()
        else:
            print(f"Configuration file {self.config_file} not found, creating default configuration")
            self._create_default_config()

    def _validate_and_fix_config(self) -> None:
        """Validate configuration and add missing sections/keys with defaults"""
        config_changed = False

        for section_name, section_data in self._defaults.items():
            # Add missing sections
            if not self.config.has_section(section_name):
                self.config.add_section(section_name)
                config_changed = True
                print(f"Added missing section: {section_name}")

            # Add missing keys with defaults
            for key, default_value in section_data.items():
                if not self.config.has_option(section_name, key):
                    self.config.set(section_name, key, str(default_value))
                    config_changed = True
                    print(f"Added missing option {section_name}/{key} with default: {default_value}")

        # Save if we made changes
        if config_changed:
            self.save_config()
            print("Configuration updated with missing defaults")

    def _create_default_config(self) -> None:
        """Create default configuration file"""
        self.config.clear()

        for section_name, section_data in self._defaults.items():
            self.config.add_section(section_name)
            for key, value in section_data.items():
                self.config.set(section_name, key, str(value))

        self.save_config()

    def save_config(self) -> None:
        """Save configuration to file"""
        try:
            # Ensure the directory exists
            self.config_file.parent.mkdir(parents=True, exist_ok=True)

            with open(self.config_file, 'w') as f:
                self.config.write(f)
            print(f"Configuration saved to {self.config_file}")
        except Exception as e:
            print(f"Error saving config file {self.config_file}: {e}")

    def get(self, section: str, key: str) -> Any:
        """
        Get a configuration value with validation

        Args:
            section: Configuration section name
            key: Configuration key name
            fallback: Fallback value if key not found

        Returns:
            Configuration value with appropriate type conversion and validation
        """
        try:
            value = self.config.get(section, key)

            # Get the expected type from defaults
            if section in self._defaults and key in self._defaults[section]:
                default_value = self._defaults[section][key]
                converted_value = self._convert_value(value, type(default_value))

                # Validate specific settings
                return self._validate_value(section, key, converted_value, default_value)

            return value
        except (configparser.NoSectionError, configparser.NoOptionError):
            # Return default value if available
            if section in self._defaults and key in self._defaults[section]:
                return self._defaults[section][key]

            return None

    def _validate_value(self, section: str, key: str, value: Any, default_value: Any) -> Any:
        """Validate configuration values and return valid value or default"""
        try:
            # Validate specific settings with ranges/constraints
            if section == "general" and key == "max_file_size_mb":
                if not isinstance(value, int) or value < 1 or value > 10000:
                    print(f"Invalid max_file_size_mb: {value}, using default: {default_value}")
                    return default_value

            elif section == "ui" and key == "font_size":
                if not isinstance(value, int) or value < 6 or value > 24:
                    print(f"Invalid font_size: {value}, using default: {default_value}")
                    return default_value

            elif section == "ui" and key == "video_volume":
                if not isinstance(value, int) or value < 0 or value > 100:
                    print(f"Invalid video_volume: {value}, using default: {default_value}")
                    return default_value

            elif section == "ui" and key == "theme":
                valid_themes = ["System Default", "Light", "Dark"]
                if value not in valid_themes:
                    print(f"Invalid theme: {value}, using default: {default_value}")
                    return default_value

            elif section == "advanced" and key == "max_tree_depth":
                if not isinstance(value, int) or value < 1 or value > 10:
                    print(f"Invalid max_tree_depth: {value}, using default: {default_value}")
                    return default_value

            elif section == "advanced" and key == "log_level":
                valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
                if value not in valid_levels:
                    print(f"Invalid log_level: {value}, using default: {default_value}")
                    return default_value

            return value

        except Exception as e:
            print(f"Error validating {section}/{key}: {e}, using default: {default_value}")
            return default_value

    def set(self, section: str, key: str, value: Any) -> None:
        """
        Set a configuration value

        Args:
            section: Configuration section name
            key: Configuration key name
            value: Value to set
        """
        if not self.config.has_section(section):
            self.config.add_section(section)

        self.config.set(section, key, str(value))

    def get_bool(self, section: str, key: str, fallback: bool = False) -> bool:
        """Get a boolean configuration value"""
        try:
            return self.config.getboolean(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return self.get(section, key, fallback)

    def get_int(self, section: str, key: str, fallback: int = 0) -> int:
        """Get an integer configuration value"""
        try:
            return self.config.getint(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return self.get(section, key, fallback)

    def get_float(self, section: str, key: str, fallback: float = 0.0) -> float:
        """Get a float configuration value"""
        try:
            return self.config.getfloat(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError, ValueError):
            return self.get(section, key, fallback)

    def _convert_value(self, value: str, target_type: type) -> Any:
        """Convert string value to target type"""
        if target_type == bool:
            return value.lower() in ('true', '1', 'yes', 'on')
        elif target_type == int:
            return int(value)
        elif target_type == float:
            return float(value)
        else:
            return value

    def get_section(self, section: str) -> Dict[str, Any]:
        """Get all values from a section"""
        result = {}
        if self.config.has_section(section):
            for key in self.config[section]:
                result[key] = self.get(section, key)
        return result

    def has_section(self, section: str) -> bool:
        """Check if section exists"""
        return self.config.has_section(section)

    def has_option(self, section: str, key: str) -> bool:
        """Check if option exists in section"""
        return self.config.has_option(section, key)

    def remove_option(self, section: str, key: str) -> bool:
        """Remove an option from a section"""
        return self.config.remove_option(section, key)

    def remove_section(self, section: str) -> bool:
        """Remove a section"""
        return self.config.remove_section(section)

    def validate_config_file(self) -> bool:
        """Validate the configuration file integrity"""
        try:
            if not self.config_file.exists():
                return False

            # Try to read the file
            test_config = configparser.ConfigParser()
            test_config.read(self.config_file)

            # Check if all required sections exist
            for section_name in self._defaults.keys():
                if not test_config.has_section(section_name):
                    print(f"Missing required section: {section_name}")
                    return False

            return True

        except Exception as e:
            print(f"Config file validation failed: {e}")
            return False

    def get_config_info(self) -> Dict[str, Any]:
        """Get information about the current configuration"""
        info = {
            'config_file': str(self.config_file),
            'file_exists': self.config_file.exists(),
            'is_valid': False,
            'sections': [],
            'missing_sections': [],
            'total_settings': 0
        }

        if info['file_exists']:
            info['is_valid'] = self.validate_config_file()
            info['sections'] = self.config.sections()
            info['missing_sections'] = [s for s in self._defaults.keys() if s not in info['sections']]
            info['total_settings'] = sum(len(self.config[section]) for section in info['sections'])

        return info


# Global configuration instance
_config_instance: Optional[ConfigLoader] = None


def get_config(config_file: Optional[str] = None) -> ConfigLoader:
    """Get the global configuration instance"""
    global _config_instance
    if _config_instance is None or config_file is not None:
        _config_instance = ConfigLoader(config_file)
    return _config_instance


def reload_config(config_file: Optional[str] = None) -> ConfigLoader:
    """Reload the configuration from file"""
    global _config_instance
    _config_instance = ConfigLoader(config_file)
    return _config_instance
