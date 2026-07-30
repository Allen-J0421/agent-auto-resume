# Release checklist

Use this checklist before creating a public GitHub release or publishing a
package.

1. Confirm `README.md` has the final GitHub repository URL.
2. Enable private vulnerability reporting in the GitHub repository settings.
3. Review open security reports and dependency alerts.
4. Run the full validation suite on macOS or Linux:

   ```bash
   python3 -m unittest discover -v
   python3 -m compileall -q agent_resume
   python3 -m pip install .
   agent-resume --version
   ```

5. Verify `agent-resume doctor` with a real authenticated provider CLI, without
   placing account information in logs or release notes.
6. Check the package contents before upload:

   ```bash
   python3 -m build
   python3 -m tarfile -l dist/*.tar.gz
   ```

7. Update the version in both `agent_resume/__init__.py` and `setup.py`, then
   add release notes describing user-visible changes and compatibility limits.
8. Tag the reviewed commit and create the GitHub release.
