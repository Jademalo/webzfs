"""
Sanoid Configuration Management Service
Manages sanoid configuration for automated ZFS snapshot scheduling
"""
import io
import os
import subprocess
import configparser
from typing import Dict, List, Any, Optional
from pathlib import Path

from services.utils import run_privileged_command, is_freebsd, is_bsd, is_linux
from services.file import save_file


class SanoidService:
    """Service for managing Sanoid snapshot scheduling"""
    
    # Platform-aware config paths
    # FreeBSD/BSD: pkg installs to /usr/local/etc/sanoid/
    # Linux: packages install to /etc/sanoid/
    # Note: Debian's sanoid package does not create /etc/sanoid/ or
    # sanoid.conf. The defaults file lives at
    # /usr/share/sanoid/sanoid.defaults.conf and is used directly by the
    # packaged sanoid executable, so WebZFS does not need to manage it.
    SANOID_CONF_LINUX = "/etc/sanoid/sanoid.conf"
    SANOID_CONF_BSD = "/usr/local/etc/sanoid/sanoid.conf"
    
    # Common paths where sanoid might be installed
    COMMON_PATHS = [
        '/usr/local/bin/sanoid',   # FreeBSD pkg install location
        '/usr/bin/sanoid',          # Linux package manager location
        '/usr/sbin/sanoid',         # Alternative Linux location
        '/usr/local/sbin/sanoid',   # Alternative FreeBSD/local location
    ]
    
    def __init__(self):
        if is_bsd():
            self.config_path = Path(self.SANOID_CONF_BSD)
        else:
            self.config_path = Path(self.SANOID_CONF_LINUX)
        
        self.sanoid_path = self._find_sanoid_path()
    
    def _write_config(self, config: configparser.ConfigParser) -> None:
        """
        Safely write the sanoid configuration file.
        
        Renders the configuration to a string, validates it by re-parsing,
        then writes it. If the config file or its parent directory is not
        directly writable (fresh Debian installs have no /etc/sanoid at
        all, and the file is normally root-owned), the write goes through
        the privileged path in services/file.py which uses sudo mkdir and
        sudo tee. Those commands are already granted to the webzfs user
        in the Linux sudoers configuration.
        
        Args:
            config: The ConfigParser object to write
        
        Raises:
            Exception: If the rendered configuration fails validation
                or the write fails. The existing config file is never
                touched unless validation succeeds.
        """
        # Render to a string first so nothing touches disk on failure
        buffer = io.StringIO()
        config.write(buffer)
        content = buffer.getvalue()
        
        # Validate the rendered content before replacing the live config
        validation_parser = configparser.ConfigParser()
        try:
            validation_parser.read_string(content)
        except configparser.Error as e:
            raise Exception(f"Generated configuration failed validation: {str(e)}")
        
        # Determine whether a direct write is possible
        config_file = str(self.config_path)
        parent_dir = self.config_path.parent
        if self.config_path.exists():
            directly_writable = os.access(config_file, os.W_OK)
        else:
            directly_writable = parent_dir.is_dir() and os.access(str(parent_dir), os.W_OK)
        
        if directly_writable:
            with open(config_file, 'w') as f:
                f.write(content)
        elif is_linux():
            # Privileged write path: sudo mkdir -p + sudo tee.
            # Creates /etc/sanoid and sanoid.conf root-owned without
            # making them writable by the webzfs user.
            save_file(config_file, content, use_sudo=True)
        else:
            raise Exception(
                f"Cannot write {config_file}: permission denied and no "
                "privileged write path is available on this platform"
            )
    
    def _find_sanoid_path(self) -> Optional[str]:
        """
        Discover the sanoid binary path.
        
        Tries 'which' first, then falls back to checking common install paths
        directly. This handles restricted PATH environments such as FreeBSD
        rc.d services where /usr/local/bin may not be in PATH.
        
        Returns:
            Full path to sanoid binary, or None if not found.
        """
        # Try to find sanoid using which first
        try:
            which_result = subprocess.run(
                ['which', 'sanoid'],
                capture_output=True,
                text=True
            )
            if which_result.returncode == 0:
                return which_result.stdout.strip()
        except Exception:
            pass
        
        # Check common paths directly (handles restricted PATH environments)
        for path in self.COMMON_PATHS:
            if Path(path).exists() and Path(path).is_file():
                return path
        
        return None
    
    def get_config(self) -> Dict[str, Any]:
        """
        Read and parse the sanoid configuration file
        
        Returns:
            Dictionary containing all configuration sections
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            result = {
                'datasets': {},
                'templates': {}
            }
            
            for section in config.sections():
                section_data = dict(config.items(section))
                
                if section.startswith('template_'):
                    result['templates'][section] = section_data
                else:
                    result['datasets'][section] = section_data
            
            return result
            
        except Exception as e:
            raise Exception(f"Failed to read sanoid configuration: {str(e)}")
    
    def get_templates(self) -> Dict[str, Dict[str, str]]:
        """
        Get all snapshot policy templates
        
        Returns:
            Dictionary of template name to template settings
        """
        try:
            config = self.get_config()
            return config.get('templates', {})
        except Exception as e:
            raise Exception(f"Failed to get templates: {str(e)}")
    
    def get_datasets(self) -> Dict[str, Dict[str, str]]:
        """
        Get all configured datasets and their policies
        
        Returns:
            Dictionary of dataset name to policy settings
        """
        try:
            config = self.get_config()
            return config.get('datasets', {})
        except Exception as e:
            raise Exception(f"Failed to get datasets: {str(e)}")
    
    def add_dataset(self, dataset_name: str, template: str, 
                   recursive: str = 'no', **kwargs) -> None:
        """
        Add a dataset to sanoid configuration
        
        Args:
            dataset_name: Name of the ZFS dataset (e.g., tank/data)
            template: Template to use (e.g., production, backup)
            recursive: Whether to include child datasets (yes/no/zfs)
            **kwargs: Additional configuration options
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            # Add or update the dataset section
            if not config.has_section(dataset_name):
                config.add_section(dataset_name)
            
            config.set(dataset_name, 'use_template', template)
            config.set(dataset_name, 'recursive', recursive)
            
            # Add any additional options
            for key, value in kwargs.items():
                config.set(dataset_name, key, str(value))
            
            self._write_config(config)
                
        except Exception as e:
            raise Exception(f"Failed to add dataset: {str(e)}")
    
    def update_dataset(self, dataset_name: str, settings: Dict[str, str]) -> None:
        """
        Update settings for an existing dataset
        
        Args:
            dataset_name: Name of the dataset
            settings: Dictionary of settings to update
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            if not config.has_section(dataset_name):
                raise Exception(f"Dataset {dataset_name} not found in configuration")
            
            for key, value in settings.items():
                config.set(dataset_name, key, str(value))
            
            self._write_config(config)
                
        except Exception as e:
            raise Exception(f"Failed to update dataset: {str(e)}")
    
    def remove_dataset(self, dataset_name: str) -> None:
        """
        Remove a dataset from sanoid configuration
        
        Args:
            dataset_name: Name of the dataset to remove
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            if not config.has_section(dataset_name):
                raise Exception(f"Dataset {dataset_name} not found in configuration")
            
            config.remove_section(dataset_name)
            
            self._write_config(config)
                
        except Exception as e:
            raise Exception(f"Failed to remove dataset: {str(e)}")
    
    def create_template(self, template_name: str, settings: Dict[str, Any]) -> None:
        """
        Create a new snapshot policy template
        
        Args:
            template_name: Name for the template (without template_ prefix)
            settings: Dictionary of template settings
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            section_name = f"template_{template_name}"
            
            if config.has_section(section_name):
                raise Exception(f"Template {template_name} already exists")
            
            config.add_section(section_name)
            
            for key, value in settings.items():
                config.set(section_name, key, str(value))
            
            self._write_config(config)
                
        except Exception as e:
            raise Exception(f"Failed to create template: {str(e)}")
    
    def update_template(self, template_name: str, settings: Dict[str, Any]) -> None:
        """
        Update an existing template
        
        Args:
            template_name: Name of the template (with or without template_ prefix)
            settings: Dictionary of settings to update
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            # Handle both template_name and name formats
            section_name = template_name if template_name.startswith('template_') else f"template_{template_name}"
            
            if not config.has_section(section_name):
                raise Exception(f"Template {template_name} not found")
            
            for key, value in settings.items():
                config.set(section_name, key, str(value))
            
            self._write_config(config)
                
        except Exception as e:
            raise Exception(f"Failed to update template: {str(e)}")
    
    def delete_template(self, template_name: str) -> None:
        """
        Delete a template
        
        Args:
            template_name: Name of the template to delete
        """
        try:
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            section_name = template_name if template_name.startswith('template_') else f"template_{template_name}"
            
            if not config.has_section(section_name):
                raise Exception(f"Template {template_name} not found")
            
            config.remove_section(section_name)
            
            self._write_config(config)
                
        except Exception as e:
            raise Exception(f"Failed to delete template: {str(e)}")
    
    def run_sanoid(self, take_snapshots: bool = True, prune_snapshots: bool = False,
                   verbose: bool = False, debug: bool = False) -> Dict[str, Any]:
        """
        Run sanoid manually.
        
        Uses the discovered full binary path to avoid 'not found' errors
        in restricted PATH environments (e.g. FreeBSD rc.d services).
        
        Args:
            take_snapshots: Take snapshots according to policy
            prune_snapshots: Prune snapshots according to policy
            verbose: Enable verbose output
            debug: Enable debug output
            
        Returns:
            Dictionary with execution results
        """
        try:
            # Re-discover path if not cached (in case installed after startup)
            if not self.sanoid_path:
                self.sanoid_path = self._find_sanoid_path()
            
            if not self.sanoid_path:
                raise Exception("sanoid binary not found. Install sanoid and restart WebZFS.")
            
            # Use the discovered full path instead of bare 'sanoid'
            cmd = [self.sanoid_path]
            
            if take_snapshots:
                cmd.append('--take-snapshots')
            
            if prune_snapshots:
                cmd.append('--prune-snapshots')
            
            if verbose:
                cmd.append('--verbose')
            
            if debug:
                cmd.append('--debug')
            
            # Use run_privileged_command to handle sudo on Linux
            result = run_privileged_command(cmd, check=False)
            
            return {
                'success': result.returncode == 0,
                'returncode': result.returncode,
                'stdout': result.stdout,
                'stderr': result.stderr
            }
            
        except Exception as e:
            raise Exception(f"Failed to run sanoid: {str(e)}")
    
    def check_sanoid_status(self) -> Dict[str, Any]:
        """
        Check if sanoid is installed and get its status.
        
        Uses the path discovered at init time. If not found at init,
        re-checks in case sanoid was installed after service start.
        
        Returns:
            Dictionary with sanoid status information
        """
        try:
            # Re-discover if not found at init (in case installed after startup)
            sanoid_path = self.sanoid_path
            if not sanoid_path:
                sanoid_path = self._find_sanoid_path()
                if sanoid_path:
                    self.sanoid_path = sanoid_path
            
            if not sanoid_path:
                return {
                    'installed': False,
                    'path': None,
                    'version': None,
                    'config_exists': False
                }
            
            # Try to get version using the found path
            try:
                version_result = subprocess.run(
                    [sanoid_path, '--version'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                version = version_result.stdout.strip() if version_result.returncode == 0 else 'unknown'
            except Exception:
                version = 'unknown'
            
            return {
                'installed': True,
                'path': sanoid_path,
                'version': version,
                'config_exists': self.config_path.exists(),
                'config_path': str(self.config_path)
            }
            
        except Exception as e:
            raise Exception(f"Failed to check sanoid status: {str(e)}")
    
    def validate_config(self) -> Dict[str, Any]:
        """
        Validate the sanoid configuration
        
        Returns:
            Dictionary with validation results
        """
        try:
            # Try to parse the config
            config = configparser.ConfigParser()
            config.read(self.config_path)
            
            errors = []
            warnings = []
            
            # Check for common issues
            dataset_sections = [s for s in config.sections() if not s.startswith('template_')]
            template_sections = [s for s in config.sections() if s.startswith('template_')]
            
            if not dataset_sections:
                warnings.append("No datasets configured")
            
            if not template_sections:
                warnings.append("No templates defined")
            
            # Check that datasets reference valid templates
            for dataset in dataset_sections:
                if config.has_option(dataset, 'use_template'):
                    templates = config.get(dataset, 'use_template').split(',')
                    for template in templates:
                        template = template.strip()
                        template_section = f"template_{template}" if not template.startswith('template_') else template
                        if template_section not in config.sections():
                            errors.append(f"Dataset '{dataset}' references non-existent template '{template}'")
            
            return {
                'valid': len(errors) == 0,
                'errors': errors,
                'warnings': warnings,
                'dataset_count': len(dataset_sections),
                'template_count': len(template_sections)
            }
            
        except Exception as e:
            return {
                'valid': False,
                'errors': [f"Failed to parse configuration: {str(e)}"],
                'warnings': [],
                'dataset_count': 0,
                'template_count': 0
            }
