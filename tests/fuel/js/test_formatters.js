// The price formatters in static/js/drive.js, extracted from the shipped file
// so this tests the real code text rather than a copy of it.
const fs = require('fs');
const path = require('path');
const REPO = path.resolve(__dirname, '../../..');
const src = fs.readFileSync(path.join(REPO, 'static/js/drive.js'), 'utf8');

const varsStart = src.indexOf('    var fuelUnits = {');
const varsEnd = src.indexOf("var MINOR_SUFFIX = { GBP: 'p' };") +
                "var MINOR_SUFFIX = { GBP: 'p' };".length;
const start = src.indexOf('    function minorSuffix(u)');
const endMark = "            : km + ' km';\n    }";
const end = src.indexOf(endMark) + endMark.length;
if (varsStart < 0 || start < 0 || end < endMark.length) {
  console.error('could not slice the formatters out of drive.js'); process.exit(2);
}

const m = { exports: {} };
new Function('module', src.slice(varsStart, varsEnd) + '\n' + src.slice(start, end) + `
  module.exports = { fuelPrice, fuelDelta, fuelAxisLabel, fuelPriceHeader,
                     fuelDistance, fuelRadiusLabel,
                     setUnits: function (u) { fuelUnits = u; } };`)(m);
const F = m.exports;

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? '    ok   ' : '    FAIL ') + name +
              (cond ? '' : '  <- ' + JSON.stringify(extra)));
  if (!cond) fails.push(name);
}

const GB = { currency:'GBP', symbol:'£',  volume:'L',      distance:'mi', display_scale:'minor', decimals:3 };
const DE = { currency:'EUR', symbol:'€',  volume:'L',      distance:'km', display_scale:'major', decimals:3 };
const US = { currency:'USD', symbol:'$',  volume:'gal_us', distance:'mi', display_scale:'major', decimals:3 };
const AU = { currency:'AUD', symbol:'A$', volume:'L',      distance:'km', display_scale:'minor', decimals:3 };

console.log('\n  UK — pence');
F.setUnits(GB);
check('tenths of a penny kept', F.fuelPrice(1.399) === '139.9p', F.fuelPrice(1.399));
check('1.599 does not round to 160', F.fuelPrice(1.599) === '159.9p', F.fuelPrice(1.599));
check('a delta carries no symbol', F.fuelDelta(0.042) === '4.2p', F.fuelDelta(0.042));
check('the axis label is bare', F.fuelAxisLabel(1.399) === '139.9');
check('the header names pence per litre', F.fuelPriceHeader() === 'Price/L (p)');
check('UK distance in miles', F.fuelDistance(8.04672) === '5.0 mi', F.fuelDistance(8.04672));
check('UK radius in miles', F.fuelRadiusLabel(40) === '25 mi', F.fuelRadiusLabel(40));
check('a null price is a dash', F.fuelPrice(null) === '—');
check('NaN is a dash', F.fuelPrice(NaN) === '—');

console.log('\n  km is still km where the region says so');
F.setUnits(DE);
check('German distance in km', F.fuelDistance(2.345) === '2.3 km');
check('German radius in km', F.fuelRadiusLabel(8) === '8 km');

console.log('\n  Germany — euro');
F.setUnits(DE);
const de = F.fuelPrice(1.719);
check('three decimals', /1[.,]719/.test(de), de);
check('a euro sign', de.includes('€'), de);
check('the header names EUR per litre', F.fuelPriceHeader() === 'Price/L (EUR)');
check('a delta is a bare number', F.fuelDelta(0.042) === '0.042');

console.log('\n  US — gallons and miles');
F.setUnits(US);
const us = F.fuelPrice(3.459);
check('dollars', us.includes('$') && /3[.,]459/.test(us), us);
check('the header names USD per gallon', F.fuelPriceHeader() === 'Price/gal (USD)');
check('distance converts to miles', F.fuelDistance(8.04672) === '5.0 mi');
check('the radius label converts', F.fuelRadiusLabel(40) === '25 mi');

console.log('\n  Australia — cents');
F.setUnits(AU);
check('a currency with no mapped suffix falls back to c',
      F.fuelPrice(1.809) === '180.9c', F.fuelPrice(1.809));
check('the header uses that suffix', F.fuelPriceHeader() === 'Price/L (c)');
check('a delta in cents', F.fuelDelta(0.031) === '3.1c', F.fuelDelta(0.031));
check('the axis is bare cents', F.fuelAxisLabel(2.039) === '203.9');

console.log('\n  bad input degrades rather than throwing');
F.setUnits({ currency:'ZZZ', symbol:'?', volume:'L', distance:'km', display_scale:'major', decimals:2 });
let zz = F.fuelPrice(1.5);
check('an unknown but valid code prints the code',
      zz.includes('ZZZ') && zz.includes('1.50'), zz);
F.setUnits({ currency:'NOPE', symbol:'?', volume:'L', distance:'km', display_scale:'major', decimals:2 });
check('a malformed code falls back to the symbol', F.fuelPrice(1.5) === '?1.50', F.fuelPrice(1.5));
F.setUnits({ currency:'', symbol:'', volume:'L', distance:'km', display_scale:'major', decimals:2 });
check('an empty currency still prints a number', F.fuelPrice(1.5) === '1.50');

console.log('\n  an explicit units argument overrides the global');
F.setUnits(GB);
check('per-call units win', F.fuelPrice(1.719, DE).includes('€'));
check('the global is untouched', F.fuelPrice(1.399) === '139.9p');

process.exit(fails.length ? 1 : 0);
