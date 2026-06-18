#!/usr/bin/env python
import re
import os

# Files with markers
files_to_fix = [
    'core/views.py',
    'core/prompt.py',
    'core/urls.py',
    'core/static/core/css/main.css',
    'templates/core/base.html',
    'templates/core/dashboard.html',
    'templates/core/result.html',
    'templates/core/session.html'
]

# Pattern to match merge markers and extract "theirs" (remote) side
marker_pattern = r'<<<<<<< HEAD\n(.*?)\n=======\n(.*?)\n>>>>>>> [^\n]+\n'

for filepath in files_to_fix:
    filepath = filepath.replace('/', os.sep)  # Handle path separators
    try:
        if not os.path.exists(filepath):
            print(f"Skipped (not found): {filepath}")
            continue
        
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        
        # Replace markers, keeping the remote side (after =======)
        fixed_content = re.sub(marker_pattern, r'\2\n', content, flags=re.DOTALL)
        
        # Handle any edge case marker lines
        fixed_content = re.sub(r'^<<<<<<< HEAD\n', '', fixed_content, flags=re.MULTILINE)
        fixed_content = re.sub(r'^=======\n', '', fixed_content, flags=re.MULTILINE)
        fixed_content = re.sub(r'^>>>>>>> [^\n]*\n', '', fixed_content, flags=re.MULTILINE)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(fixed_content)
        
        print(f"Fixed: {filepath}")
    except Exception as e:
        print(f"Error fixing {filepath}: {e}")

print("Done!")
