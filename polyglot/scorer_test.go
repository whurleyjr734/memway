package polyglot

import "testing"

func TestClampBounds(t *testing.T) {
	if clamp(-1) != 0 || clamp(2) != 1 {
		t.Fatal("clamp did not bound its input")
	}
}

func TestMeanEmpty(t *testing.T) {
	s := NewScorer(0.9)
	if s.Mean() != 0 {
		t.Fatal("empty scorer should mean 0")
	}
}

func TestMeanDecays(t *testing.T) {
	s := NewScorer(0.5)
	s.Add(0)
	s.Add(1)
	if s.Mean() <= 0.5 {
		t.Fatal("recent samples should dominate")
	}
}
