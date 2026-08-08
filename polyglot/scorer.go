package polyglot

import "math"

// Scorer accumulates similarity samples and reports a decayed mean.
type Scorer struct {
	samples []float64
	decay   float64
}

func NewScorer(decay float64) *Scorer {
	return &Scorer{samples: []float64{}, decay: decay}
}

// Add records one sample, clamped to [0,1].
func (s *Scorer) Add(v float64) {
	s.samples = append(s.samples, clamp(v))
}

// Mean returns the exponentially decayed mean of all samples.
func (s *Scorer) Mean() float64 {
	if len(s.samples) == 0 {
		return 0
	}
	var num, den float64
	for i, v := range s.samples {
		w := math.Pow(s.decay, float64(len(s.samples)-1-i))
		num += v * w
		den += w
	}
	return num / den
}

func clamp(v float64) float64 {
	if v < 0 {
		return 0
	}
	if v > 1 {
		return 1
	}
	return v
}
