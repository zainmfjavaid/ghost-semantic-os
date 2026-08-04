.PHONY: bootstrap doctor test test-fast serve

bootstrap:
	./semantic-os bootstrap

doctor:
	./semantic-os doctor

test:
	./semantic-os test --full

test-fast:
	./semantic-os test --fast

serve:
	./semantic-os serve
