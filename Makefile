.PHONY: test package

test:
	node tests/test_passport_plugin.mjs
	node tests/test_passport_relay.mjs
	node tests/test_passport_ima.mjs
	python3 -m unittest discover -s tests -p 'test_passport_hermes*.py'
	python3 -m unittest discover -s tests -p 'test_release.py'

package:
	python3 tools/release.py package build/release
